"""WP12 round 6: facts per key requirement, the whole job description and the story that
best matches the posting's first priority, for cover letters and motivation answers.
Offline doubles only (no PostgreSQL, no provider); fictional data."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re

import pytest

from interviewmaxxing_core import CandidateFact, FactVerification
from interviewmaxxing_generation.knowledge import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EmbeddingResult,
    PgKnowledgeStore,
)
from interviewmaxxing_generation.knowledge.store import (
    MAX_JOB_CONTEXT_CHUNKS,
    _requirement_cues,
    _requirement_lines,
    _states_figure,
    _years_count,
    same_claim,
    select_requirement_facts,
)
from interviewmaxxing_generation.knowledge.stories import StoryChunk

CONCEPTS = ("bakery", "search", "tracking", "report", "team", "email", "retail", "florist", "salary")


def fact(identifier: str, value: object, *, key: str = "experience", evidence: list[str] | None = None) -> CandidateFact:
    return CandidateFact(id=identifier, key=key, value=value, source="fictional resume", evidence=evidence or [],
                         verification=FactVerification.model_validate(
                             {"status": "VERIFIED", "method": "USER_CONFIRMED",
                              "verified_at": "2026-09-25T10:00:00Z"}))


# --- pure selection rules ----------------------------------------------------------------------


def test_a_resume_claim_and_the_story_fact_retelling_it_are_the_same_claim() -> None:
    resume = fact("resume_1", "Cut cost per order 31% by rebuilding offline conversion tracking for a bakery chain.")
    story = fact("sf_1", "I cut cost per order by 31% once offline conversion tracking was rebuilt "
                         "(regional bakery chain; resume: Paid Search Lead, Crumb & Co., 2023-04 to 2024-03)")
    assert same_claim(resume, story) and same_claim(story, resume)
    # A shared small number is not enough: different claims.
    assert not same_claim(fact("a", "Led a team of 2 designers for a florist."),
                          fact("b", "Raised checkout conversion from 2% to 8.5% for a bakery chain."))
    # Without figures, most of the shorter claim's words must match.
    assert same_claim(fact("c", "Wrote the weekly paid search report for the store managers."),
                      fact("d", "Wrote weekly paid search report for store managers of the bakery chain."))
    assert not same_claim(fact("e", "Wrote the weekly paid search report."), fact("f", "Ran email campaigns."))


def test_years_counts_and_figures() -> None:
    assert _years_count(fact("y1", "7 years of performance marketing experience")) == 7
    assert _years_count(fact("y2", "5+ years leading marketing teams")) == 5
    assert _years_count(fact("y3", 8, key="years_experience_total")) == 8
    assert _years_count(fact("r1", "Managed $400K a month in paid search over 3 years")) is None
    assert _years_count(fact("r2", "Grew online orders by 35% for a bakery chain")) is None
    assert _states_figure(fact("r3", "Grew online orders by 35% for a bakery chain"))
    assert _states_figure(fact("r4", "Managed $400K a month in paid search"))
    assert not _states_figure(fact("n1", "Managed B2B marketing supporting a Q4 sales pipeline."))
    assert not _states_figure(fact("n2", "Joined a bakery chain in 2024 to run paid search."))
    assert not _states_figure(fact("n3", "7 years of performance marketing experience"))


def test_each_requirement_gets_its_best_facts_figures_first_before_the_description_fills() -> None:
    tracking = fact("tracking", "Rebuilt conversion tracking for a bakery chain's paid search.")
    tracking_result = fact("tracking_result", "Cut cost per order 31% after rebuilding conversion tracking.")
    report = fact("report", "Wrote the weekly paid search report for store managers.")
    team = fact("team", "Led a team of 3 search specialists at a florist.")
    email = fact("email", "Ran lifecycle email for a bakery chain.")
    pools = [[tracking, tracking_result, email], [report, team, email]]
    chosen, receipt = select_requirement_facts(pools, [email, report, team], requirements=[
        "Own conversion tracking for paid search.", "Report results to the sales team weekly."], limit=12)
    # Figures first within a requirement's pool; each requirement's first pick before any second.
    assert [f.id for f in chosen] == ["tracking_result", "team", "tracking", "report", "email"]
    assert receipt["per_requirement_ids"] == [["tracking_result", "tracking"], ["team", "report"]]
    assert receipt["mode"] == "per_requirement" and receipt["limit"] == 12
    # The limit binds the round-robin before the fill.
    chosen, _ = select_requirement_facts(pools, [email], requirements=["a requirement line", "another one"], limit=3)
    assert [f.id for f in chosen] == ["tracking_result", "team", "tracking"]


def test_twins_and_extra_or_short_years_facts_are_left_out() -> None:
    resume = fact("resume_1", "Cut cost per order 31% by rebuilding offline conversion tracking for a bakery chain.")
    twin = fact("sf_twin", "I cut cost per order by 31% after rebuilding offline conversion tracking "
                           "(bakery chain; resume: Crumb & Co., 2023-04 to 2024-03)")
    growth_years = fact("years_growth", "7 years of growth marketing experience")
    team_years = fact("years_team", "5 years leading marketing teams")
    total = fact("years_total", 8, key="years_experience_total")
    requirements = ["10+ years of experience in growth marketing for consumer brands.",
                    "5+ years leading marketing teams through hiring and planning.",
                    "Own offline conversion tracking for paid channels."]
    pools = [[growth_years, total], [team_years], [resume, twin]]
    chosen, receipt = select_requirement_facts(pools, [twin, total], requirements=requirements, limit=12)
    ids = [f.id for f in chosen]
    # "7 years of growth marketing" is below the 10+ growth ask, 8 total is below the largest
    # ask it shares no words with, "5 years leading teams" meets its own 5+ ask: one years fact.
    assert ids == ["years_team", "resume_1"]
    assert receipt["skipped"] == {"same_claim": ["sf_twin"], "years_below_ask": ["years_growth", "years_total"],
                                  "second_years": []}
    assert receipt["years_asks"] == [5, 10]
    # Without a stated ask the first years fact is kept and a second is not.
    chosen, receipt = select_requirement_facts([[growth_years], [team_years]], [],
                                               requirements=["Own paid growth.", "Lead the team."], limit=12)
    assert [f.id for f in chosen] == ["years_growth"] and receipt["skipped"]["second_years"] == ["years_team"]


def test_requirement_lines_are_each_kept_once_and_bounded() -> None:
    evidence = [{"text": "About us: we bake bread. You will own paid search for the region.\n"
                         "You will own paid search for the region.\n- Strong reporting skills for the sales team"},
                {"text": "Salary range: fifty to sixty. We are an equal opportunity employer."}]
    assert _requirement_lines(evidence) == ["You will own paid search for the region.",
                                            "Strong reporting skills for the sales team"]
    assert _requirement_cues("You will own paid search. Strong reporting skills. Salary range: fifty.") == 2


# --- the store ---------------------------------------------------------------------------------


class ConceptEmbedder:
    """One dimension per fictional concept: a text's vector counts its concept words."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            words = re.findall(r"[a-z]+", text.lower())
            vector = [float(sum(word.startswith(concept) for word in words)) for concept in CONCEPTS]
            vectors.append(vector + [0.0] * (EMBEDDING_DIMENSIONS - len(vector) - 1) + [0.001])
        return EmbeddingResult(vectors, {"model": EMBEDDING_MODEL, "dimensions": EMBEDDING_DIMENSIONS,
                                         "input_count": len(texts), "batch_count": 1, "duration_ms": 0.1,
                                         "usage": {"cost": 0.00001}})


class ConceptPg:
    """The store's SQL shapes over dictionaries, ranking hybrid queries by cosine distance."""

    def __init__(self) -> None:
        self.sources: dict[tuple[str, ...], dict[str, object]] = {}
        self.chunks: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, object]] = []
        self.results: list[dict[str, object]] = []
        self.rowcount = 0

    def __call__(self) -> ConceptPg:
        return self

    def __enter__(self) -> ConceptPg:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def transaction(self) -> ConceptPg:
        return self

    def cursor(self) -> ConceptPg:
        return self

    @staticmethod
    def _key(row: dict[str, object]) -> tuple[object, ...]:
        return tuple(row[name] for name in ("candidate_id", "kind", "job_scope", "source_id"))

    def execute(self, sql: str, params: object = ()) -> None:
        params = tuple(params)  # type: ignore[arg-type]
        self.calls.append((sql, params))
        normalized = " ".join(sql.split())
        self.rowcount = 0
        if normalized.startswith("DELETE"):
            if "ANY" in sql:
                doomed = [k for k in self.sources if k[0] == params[0] and k[1] == "fact" and k[3] not in params[1]]
            elif "source_id <>" in sql:
                doomed = [k for k in self.sources if k[:3] == params[:3] and k[3] != params[3]]
            else:
                doomed = [params] if params in self.sources else []
            for key in doomed:
                del self.sources[key]
            self.chunks = {i: r for i, r in self.chunks.items() if self._key(r) not in doomed}
        elif normalized.startswith("INSERT INTO imx_knowledge.sources"):
            fields = ("candidate_id", "kind", "job_scope", "source_id", "source_version", "source_url",
                      "indexed_job_id")
            self.sources[params[:4]] = dict(zip(fields, params, strict=True))
        elif normalized.startswith("SELECT s.source_id"):
            self.results = []
            for key, source in self.sources.items():
                if key[:3] == params:
                    rows = sorted((r for r in self.chunks.values() if self._key(r) == key
                                   and r["source_version"] == source["source_version"]),
                                  key=lambda r: r["chunk_number"])  # type: ignore[arg-type,return-value]
                    self.results.append({"source_id": source["source_id"], "source_version": source["source_version"],
                                         "chunk_hashes": [r["content_hash"] for r in rows],
                                         "models": [r["embedding_model"] for r in rows]})
        elif normalized.startswith("WITH current_job"):
            sources = [s for k, s in self.sources.items() if k[:3] == (params[0], "job", params[1])]
            rows = []
            if sources:
                source = sources[-1]
                rows = [{**r, "source_url": source["source_url"], "indexed_job_id": source["indexed_job_id"]}
                        for r in self.chunks.values() if self._key(r) == self._key(source)
                        and r["source_version"] == source["source_version"]]
            self.results = sorted(rows, key=lambda r: r["chunk_number"])[:params[-1]]  # type: ignore[arg-type,return-value]
        elif normalized.startswith("WITH scoped"):
            query = json.loads(params[4])  # type: ignore[arg-type]
            rows = []
            for row in self.chunks.values():
                source = self.sources.get(self._key(row))
                if source and row["source_version"] == source["source_version"] and self._key(row)[:3] == params[:3]:
                    stored = json.loads(row["embedding"])  # type: ignore[arg-type]
                    norm = math.sqrt(sum(x * x for x in stored) * sum(x * x for x in query))
                    score = sum(x * y for x, y in zip(stored, query, strict=True)) / norm
                    rows.append((-score, row["id"], {**row, "source_url": source["source_url"],
                                                    "indexed_job_id": source["indexed_job_id"],
                                                    "score": round(score, 6)}))
            self.results = [row for _, _, row in sorted(rows)][:params[-1]]  # type: ignore[misc]

    def executemany(self, sql: str, rows: list[tuple[object, ...]]) -> None:
        fields = ("id", "candidate_id", "kind", "job_scope", "source_id", "source_version", "chunk_number",
                  "body", "content_hash", "embedding", "embedding_model")
        for row in rows:
            self.chunks[str(row[0])] = dict(zip(fields, row, strict=True))

    def fetchall(self) -> list[dict[str, object]]:
        return self.results


@pytest.fixture
def profile(fictional_candidate):
    facts = [fact("bakery_search", "Ran bakery search campaigns."),
             fact("bakery_search_2", "Optimized bakery search keywords for bakery stores."),
             fact("bakery_search_3", "Managed bakery bids."),
             fact("tracking", "Rebuilt bakery tracking so only paid orders counted, cutting cost per order 31%."),
             fact("florist_report", "Wrote the florist's weekly report for store managers.")]
    return fictional_candidate.model_copy(update={"facts": facts, "experience": [], "education": []})


def test_a_cover_letter_retrieves_facts_per_requirement(profile, mock_job) -> None:
    db, embedder = ConceptPg(), ConceptEmbedder()
    store = PgKnowledgeStore(db, embedder)
    store.index_candidate(profile)
    description = ("You will own bakery search campaigns across the region. "
                   "You will build tracking that shows which orders came from search. "
                   "Strong report writing for store managers is required.")
    store.index_job(profile.id, mock_job, description, "https://example.invalid/bakery-role")
    embedder.calls.clear()
    letter = store.retrieve(candidate=profile, job=mock_job, query="Cover letter", limit=3)
    # One embedding request: the description query and one input per key requirement.
    assert len(embedder.calls) == 1 and len(embedder.calls[0]) == 4
    assert embedder.calls[0][1:] == ["You will own bakery search campaigns across the region.",
                                     "You will build tracking that shows which orders came from search.",
                                     "Strong report writing for store managers is required."]
    # Each requirement's best fact, even though the search facts dominate the whole description.
    assert [f.id for f in letter.facts] == ["bakery_search", "tracking", "florist_report"]
    selection = letter.receipt["fact_selection"]
    assert selection["mode"] == "per_requirement" and selection["requirements"] == 3
    assert letter.receipt["counts"]["requirements"] == 3
    assert [q["stage"] for q in letter.receipt["fact_queries"]] == ["job_context", "requirement", "requirement",
                                                                    "requirement"]
    assert description not in json.dumps(letter.receipt)
    # A specific question still ranks by its own words.
    answer = store.retrieve(candidate=profile, job=mock_job, query="Describe your bakery search work.", limit=3)
    assert answer.receipt["fact_selection"] == {"mode": "ranked", "limit": 3}
    assert "tracking" not in {f.id for f in answer.facts}


def test_a_long_description_sends_its_requirement_chunks_in_order(profile, mock_job) -> None:
    db, embedder = ConceptPg(), ConceptEmbedder()
    store = PgKnowledgeStore(db, embedder)
    filler = "We bake bread and cakes for the region every morning and deliver them by van. "
    sections = [
        filler * 22,
        ("You will own paid search. " * 3 + "You must build tracking for orders. " * 3 + filler * 16),
        filler * 22,
        ("Strong reporting skills are required. You will lead a team of two. " * 3 + filler * 16),
        "Salary range: fifty to sixty thousand. We are an equal opportunity employer. " * 20,
        filler * 22,
        ("Hands-on experience with retail media is required. " * 3 + filler * 16),
    ]
    description = "\n".join(sections)
    store.index_job(profile.id, mock_job, description, "https://example.invalid/long-role")
    letter = store.retrieve(candidate=profile, job=mock_job, query="Cover letter")
    assert len(letter.job_evidence) == MAX_JOB_CONTEXT_CHUNKS
    texts = [item["text"] for item in letter.job_evidence]
    assert not any(text.startswith("Salary range") for text in texts)
    requirement_chunks = [text for text in texts if _requirement_cues(text)]
    assert len(requirement_chunks) >= 3 and texts == sorted(texts, key=description.index)
    # A short description is sent whole, in order.
    short = mock_job.model_copy(update={"normalized_url": "https://example.invalid/short"})
    store.index_job(profile.id, short, "You will own paid search. Salary: fifty.",
                    "https://example.invalid/short-role")
    assert [e["text"] for e in store.retrieve(candidate=profile, job=short, query="Cover letter").job_evidence] == [
        "You will own paid search. Salary: fifty."]
    # A specific question keeps the question-ranked, at most four chunks.
    assert len(store.retrieve(candidate=profile, job=mock_job, query="What retail media have you run?").job_evidence) <= 4


def _story(number: int, title: str, body: str) -> StoryChunk:
    text = f"Story {number:02d}: {title} | employer: fictional employer | period: 2024 | themes: work\n{body}"
    return StoryChunk(id="story:" + hashlib.sha256(text.encode()).hexdigest(), story_id=f"s{number}",
                      number=number, kind="story", title=title, employer="fictional employer", period="2024",
                      themes=("work",), text=text)


def test_the_story_best_matching_the_first_priority_is_listed_first(profile, mock_job) -> None:
    db, embedder = ConceptPg(), ConceptEmbedder()
    store = PgKnowledgeStore(db, embedder)
    store.index_candidate(profile)
    chunks = [_story(1, "Florist reports", "I wrote the florist report every week; the report changed budgets."),
              _story(2, "Email for a bakery", "I ran email for a bakery; email revenue grew."),
              _story(3, "Tracking the bakery orders", "The tracking counted calls as orders, so I rebuilt tracking.")]
    store.index_stories(profile.id, chunks, version="d" * 64)
    description = ("You will build tracking so every order is counted. "
                   "Strong report writing and email skills are required.")
    store.index_job(profile.id, mock_job, description, "https://example.invalid/tracking-role")
    result = store.retrieve(candidate=profile, job=mock_job, query="Cover letter", narrative=True)
    assert result.story_chunks[0]["title"] == "Tracking the bakery orders"
    assert result.receipt["story_priority_ids"][0] == result.story_chunks[0]["id"]
    assert "story_priority" in result.receipt["embedding_stages"][0]["query_stages"]
    assert len({c["id"] for c in result.story_chunks}) == len(result.story_chunks) <= 4


def test_hostile_requirement_hits_are_rejected(profile, mock_job) -> None:
    db, embedder = ConceptPg(), ConceptEmbedder()
    store = PgKnowledgeStore(db, embedder)
    store.index_candidate(profile)
    store.index_job(profile.id, mock_job, "You will own bakery search campaigns.", "https://example.invalid/r")
    original = db.execute

    def tampering(sql: str, params: object = ()) -> None:
        original(sql, params)
        if " ".join(sql.split()).startswith("WITH scoped") and params[1] == "fact":  # type: ignore[index]
            db.results = [replace_body(row) for row in db.results]

    def replace_body(row: dict[str, object]) -> dict[str, object]:
        tampered = copy.deepcopy(row)
        tampered["body"] = "Tampered text"
        return tampered

    db.execute = tampering  # type: ignore[method-assign]
    result = store.retrieve(candidate=profile, job=mock_job, query="Cover letter")
    assert result.facts == [] and result.receipt["rejected_count"] > 0


def test_a_letter_gets_two_voice_passages_from_the_owners_posts(profile, mock_job) -> None:
    db, embedder = ConceptPg(), ConceptEmbedder()
    store = PgKnowledgeStore(db, embedder)
    store.index_candidate(profile)
    store.index_job(profile.id, mock_job, "You will own bakery search campaigns.", "https://example.invalid/r")
    for number, post in enumerate(("Bakery search waste, fictional 2017 post.", "Tracking orders, fictional post.",
                                   "Reports nobody reads, fictional post."), 1):
        store.index_voice(profile.id, post, f"voice:blog-2017-{number}")
    result = store.retrieve(candidate=profile, job=mock_job, query="Cover letter", narrative=True)
    assert len(result.voice_samples) == 2 and not result.receipt["voice_from_stories"]
    assert all("fictional" in sample for sample in result.voice_samples)
    assert not any(sample in json.dumps(result.receipt) for sample in result.voice_samples)


def test_the_long_form_story_leads_over_a_linkedin_bullet(profile, mock_job) -> None:
    from interviewmaxxing_generation.knowledge.stories import DEFAULT_STORY_SOURCE

    db, embedder = ConceptPg(), ConceptEmbedder()
    store = PgKnowledgeStore(db, embedder)
    store.index_candidate(profile)
    bullet = _story(1, "Tracking bullet", "Rebuilt tracking tracking tracking for orders.")
    long_form = _story(2, "The dashboard that lied", "The dashboard counted calls as orders while the ledger "
                                                     "showed thin months, so I rebuilt tracking.")
    store.index_stories(profile.id, [bullet], version="e" * 64, source_id="candidate-stories-linkedin",
                        replace_others=False)
    store.index_stories(profile.id, [long_form], version="f" * 64, source_id=DEFAULT_STORY_SOURCE,
                        replace_others=False)
    store.index_job(profile.id, mock_job, "You will build tracking so every order is counted.",
                    "https://example.invalid/tracking-role")
    result = store.retrieve(candidate=profile, job=mock_job, query="Cover letter", narrative=True)
    # The bullet matches the priority's words more closely; the long-form story still leads.
    assert [c["title"] for c in result.story_chunks][:2] == ["The dashboard that lied", "Tracking bullet"]
    assert result.receipt["story_priority_ids"][0] == long_form.id
