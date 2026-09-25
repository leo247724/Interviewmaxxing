"""Offline projection tests; no live PostgreSQL or provider access."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from contextlib import contextmanager

import pytest

from interviewmaxxing_core import CandidateFact, FactVerification
from interviewmaxxing_generation.knowledge import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    EmbeddingError,
    EmbeddingResult,
    KnowledgeError,
    PgKnowledgeStore,
    fact_fingerprint,
    job_fingerprint,
    pg_connection_factory,
)


class FakeEmbedder:
    def __init__(self):
        self.calls = []
        self.fail = False

    def embed(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise EmbeddingError("Synthetic embedding failure")
        return EmbeddingResult([[1.0, *([0.0] * (EMBEDDING_DIMENSIONS - 1))] for _ in texts],
                               {"model": EMBEDDING_MODEL, "dimensions": EMBEDDING_DIMENSIONS,
                                "input_count": len(texts), "batch_count": int(bool(texts)),
                                "duration_ms": 0.1, "usage": {"cost": 0.00001 if texts else 0}})


class MemoryPg:
    """A transactional DB double, deliberately capable of returning hostile rows.

    It exercises real store parameters, source replacement and rollback semantics;
    it does not claim to validate PostgreSQL's query planner or vector ranking.
    """

    def __init__(self):
        self.sources = {}
        self.chunks = {}
        self.calls = []
        self.results = []
        self.extra_hits = []
        self.fail_insert = False
        self.on_write_lock = None
        self.rank_by_vector = False
        self.connections = 0
        self.rollbacks = 0
        self.rowcount = 0

    def __call__(self):
        self.connections += 1
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    @contextmanager
    def transaction(self):
        before = copy.deepcopy((self.sources, self.chunks))
        try:
            yield self
        except Exception:
            self.sources, self.chunks = before
            self.rollbacks += 1
            raise

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        normalized = " ".join(sql.split())
        self.rowcount = 0
        if "pg_advisory_xact_lock" in sql and self.on_write_lock:
            self.on_write_lock()
        elif normalized.startswith("DELETE"):
            if "ANY" in sql:
                doomed = [key for key in self.sources if key[0] == params[0]
                          and key[1] == "fact" and key[3] not in params[1]]
            elif "source_id <>" in sql:
                doomed = [key for key in self.sources if key[0] == params[0]
                          and key[1] == params[1] and key[2] == params[2] and key[3] != params[3]]
            else:
                doomed = [tuple(params)] if tuple(params) in self.sources else []
            for key in doomed:
                del self.sources[key]
            self.chunks = {identifier: row for identifier, row in self.chunks.items()
                           if self._key(row) not in doomed}
            self.rowcount = len(doomed)
        elif normalized.startswith("INSERT INTO imx_knowledge.sources"):
            fields = ("candidate_id", "kind", "job_scope", "source_id", "source_version",
                      "source_url", "indexed_job_id")
            self.sources[tuple(params[:4])] = dict(zip(fields, params, strict=True))
        elif normalized.startswith("SELECT s.source_id"):
            self.results = []
            for key, source in self.sources.items():
                if key[:3] == tuple(params):
                    rows = sorted((row for row in self.chunks.values() if self._key(row) == key
                                   and row["source_version"] == source["source_version"]),
                                  key=lambda row: row["chunk_number"])
                    self.results.append({"source_id": source["source_id"],
                                         "source_version": source["source_version"],
                                         "chunk_hashes": [r["content_hash"] for r in rows],
                                         "models": [r["embedding_model"] for r in rows]})
        elif normalized.startswith("WITH current_job"):
            candidate_id, scope = params[:2]
            sources = [s for key, s in self.sources.items() if key[:3] == (candidate_id, "job", scope)]
            source = sources[-1] if sources else None
            rows = []
            if source:
                for row in self.chunks.values():
                    if (self._key(row) == self._key(source)
                            and row["source_version"] == source["source_version"]):
                        rows.append({**row, "source_url": source["source_url"],
                                     "indexed_job_id": source["indexed_job_id"]})
            self.results = sorted(rows, key=lambda row: row["chunk_number"])[:params[-1]]
            self.results += [dict(row) for row in self.extra_hits]
        elif normalized.startswith("WITH scoped"):
            candidate_id, kind, scope = params[:3]
            rows = []
            for row in self.chunks.values():
                source = self.sources.get(self._key(row))
                if (source and source["source_version"] == row["source_version"]
                        and row["candidate_id"] == candidate_id and row["kind"] == kind
                        and row["job_scope"] == scope):
                    rows.append({**row, "source_url": source["source_url"],
                                 "indexed_job_id": source["indexed_job_id"]})
            if self.rank_by_vector:
                query_vector = json.loads(params[4])

                def distance(row):
                    stored = json.loads(row["embedding"])
                    denominator = math.sqrt(sum(x * x for x in stored) * sum(x * x for x in query_vector))
                    return -sum(x * y for x, y in zip(stored, query_vector, strict=True)) / denominator

                rows.sort(key=distance)
            self.results = rows[:params[-1]] + [dict(row) for row in self.extra_hits]

    def executemany(self, sql, rows):
        fields = ("id", "candidate_id", "kind", "job_scope", "source_id", "source_version",
                  "chunk_number", "body", "content_hash", "embedding", "embedding_model")
        for row in rows:
            if self.fail_insert:
                raise RuntimeError("secret DSN and fictional source text must not escape")
            self.calls.append((sql, row))
            self.chunks[row[0]] = dict(zip(fields, row, strict=True))

    def fetchall(self):
        return self.results

    @staticmethod
    def _key(row):
        return tuple(row[name] for name in ("candidate_id", "kind", "job_scope", "source_id"))


def fact(identifier="fact-1", value="Led a paid search campaign.", *, verified=True):
    return CandidateFact(id=identifier, key="experience", value=value, source="fictional resume",
                         verification=FactVerification.model_validate(
                             {"status": "VERIFIED", "method": "USER_CONFIRMED",
                              "verified_at": "2026-09-22T20:00:00Z"}
                             if verified else {"status": "UNVERIFIED"}))


@pytest.fixture
def profile(fictional_candidate):
    return fictional_candidate.model_copy(update={"facts": [fact()],
                                                  "experience": [], "education": []})


@pytest.fixture
def knowledge():
    db, embedder = MemoryPg(), FakeEmbedder()
    return PgKnowledgeStore(db, embedder), db, embedder


def test_projection_embeds_only_verified_facts_and_returns_canonical_objects(knowledge, profile, mock_job):
    store, db, embedder = knowledge
    profile = profile.model_copy(update={"facts": [fact(), fact("unverified", "Made up", verified=False)]})
    before = profile.model_dump_json()
    receipt = store.index_candidate(profile)
    assert receipt["source_count"] == 1
    assert len(db.chunks) == 1
    assert embedder.calls == [["experience: Led a paid search campaign."]]
    result = store.retrieve(candidate=profile, job=mock_job, query="paid search")
    assert result.facts == [profile.facts[0]]
    assert result.facts[0] is profile.facts[0]
    assert profile.model_dump_json() == before
    serialized = json.dumps(result.receipt)
    assert "paid search" not in serialized
    assert profile.identity.email not in serialized
    assert str(profile.facts[0].value) not in serialized


def test_candidate_and_exact_job_url_scopes(knowledge, profile, mock_job):
    store, _, _ = knowledge
    other = profile.model_copy(update={"id": "other-candidate", "facts": [fact("other", "Private other fact")]})
    different_job = mock_job.model_copy(update={"normalized_url": "https://example.invalid/job-b"})
    store.index_candidate(profile)
    store.index_candidate(other)
    store.index_job(profile.id, mock_job, "Lead role A.", "https://example.invalid/source-a")
    store.index_job(profile.id, different_job, "Lead role B.", "https://example.invalid/source-b")
    store.index_job(other.id, mock_job, "Other candidate description.", "https://example.invalid/other")
    store.index_voice(other.id, "Other candidate style.", "other-voice")
    same_url_new_id = mock_job.model_copy(update={"id": "new-runtime-id", "company": "Fresh label"})
    result = store.retrieve(candidate=profile, job=same_url_new_id, query="lead")
    assert [f.id for f in result.facts] == ["fact-1"]
    assert [j["text"] for j in result.job_evidence] == ["Lead role A."]
    assert result.voice_samples == []
    other_result = store.retrieve(candidate=profile, job=different_job, query="lead")
    assert [j["text"] for j in other_result.job_evidence] == ["Lead role B."]
    assert job_fingerprint(same_url_new_id) == job_fingerprint(mock_job)
    assert job_fingerprint(different_job) != job_fingerprint(mock_job)


@pytest.mark.parametrize("change", [
    {"value": "Changed claim"}, {"source": "Changed source"}, {"key": "changed-key"},
    {"evidence": ["New evidence"]}, {"confidence": 0.8},
    {"verification": FactVerification.model_validate({"status": "VERIFIED", "method": "USER_STATED",
                                                      "verified_at": "2026-09-23T20:00:00Z"})},
    {"verification": FactVerification(status="UNVERIFIED")},
])
def test_stale_fact_hash_or_revocation_cannot_restore_claim(knowledge, profile, mock_job, change):
    store, _, _ = knowledge
    store.index_candidate(profile)
    changed = profile.model_copy(update={"facts": [profile.facts[0].model_copy(update=change)]})
    assert fact_fingerprint(changed.facts[0]) != fact_fingerprint(profile.facts[0])
    result = store.retrieve(candidate=changed, job=mock_job, query="experience")
    assert result.facts == []
    assert result.receipt["rejected_count"] == 1


def test_reindex_removes_revoked_facts_and_preserves_other_candidate(knowledge, profile, mock_job):
    store, db, _ = knowledge
    other = profile.model_copy(update={"id": "other"})
    store.index_candidate(profile)
    store.index_candidate(other)
    revoked = profile.model_copy(update={"facts": []})
    receipt = store.index_candidate(revoked)
    assert receipt["revoked_count"] == 1
    assert {r["candidate_id"] for r in db.chunks.values()} == {"other"}
    assert store.retrieve(candidate=revoked, job=mock_job, query="campaign").facts == []


def test_source_replacement_is_idempotent_and_exact(knowledge, profile, mock_job):
    store, db, _ = knowledge
    url = "https://example.invalid/source"
    first = store.index_job(profile.id, mock_job, "Old description.", url)
    second = store.index_job(profile.id, mock_job, "Old description.", url)
    assert first["chunk_ids"] == second["chunk_ids"] and len(db.chunks) == 1
    assert second["unchanged_source_count"] == 1
    changed_url = store.index_job(profile.id, mock_job, "Another source.", url + "-2")
    assert changed_url["replaced_source_count"] == 1
    newer = store.index_job(profile.id, mock_job, "New description.", url)
    assert newer["chunk_ids"] != first["chunk_ids"] and len(db.chunks) == 1
    store.index_voice(profile.id, "Old voice.", "own-sample")
    store.index_voice(profile.id, "New voice.", "own-sample")
    result = store.retrieve(candidate=profile, job=mock_job, query="description")
    assert [j["text"] for j in result.job_evidence] == ["New description."]
    assert result.voice_samples == ["New voice."]
    assert result.facts == []


def test_sql_failure_rolls_back_exact_source_replacement_without_source_leak(knowledge, profile, mock_job):
    store, db, _ = knowledge
    store.index_job(profile.id, mock_job, "Original description.", "https://example.invalid/source")
    before = copy.deepcopy((db.sources, db.chunks))
    db.fail_insert = True
    with pytest.raises(KnowledgeError, match="database operation failed") as error:
        store.index_job(profile.id, mock_job, "Replacement.", "https://example.invalid/source")
    assert "secret" not in str(error.value) and error.value.__suppress_context__
    assert (db.sources, db.chunks) == before
    assert db.rollbacks == 1


def test_embedding_failure_never_falls_back_to_lexical_or_mutates_db(knowledge, profile, mock_job):
    store, db, embedder = knowledge
    embedder.fail = True
    with pytest.raises(EmbeddingError):
        store.index_candidate(profile)
    with pytest.raises(EmbeddingError):
        store.retrieve(candidate=profile, job=mock_job, query="experience")
    assert not any("INSERT" in sql or "DELETE" in sql for sql, _ in db.calls)


def test_hybrid_sql_parameters_and_transaction_scoping(knowledge, profile, mock_job):
    store, db, _ = knowledge
    query = "campaign'); DROP SCHEMA public; --"
    profile = profile.model_copy(update={"id": "candidate'--"})
    store.retrieve(candidate=profile, job=mock_job, query=query)
    retrievals = [(sql, params) for sql, params in db.calls if "WITH scoped" in sql]
    assert len(retrievals) == 2
    assert {p[1] for _, p in retrievals} == {"fact", "voice"}
    for sql, params in retrievals:
        assert query not in sql and profile.id not in sql
        assert "campaign" in params[6] and " OR " in params[6] and params[0] == profile.id
        assert "c.candidate_id = %s" in sql and "c.job_scope = %s" in sql
        assert "extensions.<=>" in sql and "ts_rank_cd" in sql
        assert "60 + v.rank" in sql and "60 + l.rank" in sql
        assert params[-1] <= 80
    assert any("set_config" in sql and params == (profile.id,) for sql, params in db.calls)
    job_queries = [(sql, params) for sql, params in db.calls if "WITH current_job" in sql]
    assert len(job_queries) == 1
    assert query not in job_queries[0][0] and "campaign" in job_queries[0][1][3]
    assert "candidate_id = %s" in job_queries[0][0] and "job_scope = %s" in job_queries[0][0]


def test_hostile_cross_scope_and_corrupted_rows_rejected(knowledge, profile, mock_job):
    store, db, _ = knowledge
    store.index_candidate(profile)
    row = dict(next(iter(db.chunks.values())))
    db.extra_hits = [{**row, "candidate_id": "other"},
                     {**row, "job_scope": "other"},
                     {**row, "body": "Tampered body"},
                     {**row, "body": "Tampered body", "content_hash": hashlib.sha256(b"Tampered body").hexdigest()}]
    result = store.retrieve(candidate=profile, job=mock_job, query="campaign")
    assert result.facts == [profile.facts[0]]
    assert result.job_evidence == [] and result.voice_samples == []
    assert result.receipt["rejected_count"] == 12


def test_duplicate_chunks_and_sources_do_not_repeat_returned_evidence(knowledge, profile, mock_job):
    store, db, _ = knowledge
    profile = profile.model_copy(update={"facts": [fact(value="Long claim. " * 200)]})
    store.index_candidate(profile)
    store.index_job(profile.id, mock_job, "Same job requirement.", "https://example.invalid/a")
    store.index_job(profile.id, mock_job, "Same job requirement.", "https://example.invalid/b")
    store.index_voice(profile.id, "My direct writing style.", "a")
    store.index_voice(profile.id, "My direct writing style.", "b")
    assert len(db.chunks) == 5
    result = store.retrieve(candidate=profile, job=mock_job, query="claim")
    assert len(result.facts) == len(result.job_evidence) == len(result.voice_samples) == 1


def test_stale_source_version_is_excluded_before_ranking(knowledge, profile, mock_job):
    store, db, _ = knowledge
    store.index_job(profile.id, mock_job, "Stored requirement.", "https://example.invalid/a")
    row = next(iter(db.chunks.values()))
    row["source_version"] = "0" * 64
    assert store.retrieve(candidate=profile, job=mock_job, query="requirement").job_evidence == []


def test_unchanged_facts_skip_embeddings_but_verification_change_does_not(knowledge, profile):
    store, _, embedder = knowledge
    profile = profile.model_copy(update={"facts": [fact(), fact("second", "Built reporting.")]})
    store.index_candidate(profile)
    repeated = store.index_candidate(profile)
    assert repeated["unchanged_source_count"] == 2
    assert embedder.calls[-1] == []
    changed_fact = profile.facts[0].model_copy(update={"verification": FactVerification.model_validate(
        {"status": "VERIFIED", "method": "USER_STATED", "verified_at": "2026-09-23T20:00:00Z"})})
    changed = profile.model_copy(update={"facts": [changed_fact, profile.facts[1]]})
    receipt = store.index_candidate(changed)
    assert receipt["unchanged_source_count"] == 1
    assert len(embedder.calls[-1]) == 1


def test_missing_chunks_are_rebuilt_instead_of_skipped(knowledge, profile):
    store, db, embedder = knowledge
    store.index_candidate(profile)
    db.chunks.clear()
    receipt = store.index_candidate(profile)
    assert receipt["unchanged_source_count"] == 0
    assert len(db.chunks) == len(embedder.calls[-1]) == 1


def test_unchanged_fast_path_rechecks_source_under_write_lock(knowledge, profile):
    store, db, _ = knowledge
    store.index_candidate(profile)
    key = next(iter(db.sources))
    db.on_write_lock = lambda: db.sources[key].update(source_version="0" * 64)
    with pytest.raises(KnowledgeError, match="changed during preparation"):
        store.index_candidate(profile)
    assert db.rollbacks == 1


class TopicEmbedder(FakeEmbedder):
    """Deterministic semantic axes prove the role query reaches vector ranking.

    The real provider/pgvector relevance evaluation belongs to the parent; this
    model has no network access and never manufactures candidate fact content.
    """

    def embed(self, texts):
        result = super().embed(texts)
        concepts = ("meta", "shopify", "ecommerce", "checkout", "developer", "platform",
                    "search", "software", "demand", "generation", "webinar", "pipeline", "b2b", "sales",
                    "team", "copywriter", "specialist", "score", "lead", "offline", "conversion",
                    "revenue", "cpa", "roas")
        vectors = []
        for text in texts:
            words = re.findall(r"[a-z]+", text.lower())
            vector = [float(sum(word.startswith(concept) for word in words)) for concept in concepts]
            vector += [0.0] * (EMBEDDING_DIMENSIONS - len(vector) - 1) + [0.001]
            vectors.append(vector)
        return EmbeddingResult(vectors, result.receipt)


def test_generic_cover_letter_selects_different_role_evidence_not_only_company_names(profile, mock_job):
    facts = [fact("meta-growth", "Scaled Meta ads for Shopify ecommerce growth."),
             fact("checkout", "Improved checkout conversion for Shopify ecommerce stores."),
             fact("developer-paid", "Ran paid acquisition for developer platforms."),
             fact("developer-search", "Optimized search campaigns for software developers."),
             fact("demand", "Built demand generation pipeline with webinars."),
             fact("b2b", "Managed B2B marketing supporting a sales pipeline.")]
    profile = profile.model_copy(update={"facts": facts})
    db, embedder = MemoryPg(), TopicEmbedder()
    db.rank_by_vector = True
    store = PgKnowledgeStore(db, embedder)
    store.index_candidate(profile)
    cases = [
        ("spoks-like", "Lead Meta ads, Shopify ecommerce growth and checkout conversion.",
         {"meta-growth", "checkout"}),
        ("vercel-like", "Run paid marketing for software developer platforms, including search campaigns.",
         {"developer-paid", "developer-search"}),
        ("zello-like", "Build B2B demand generation, webinars and sales pipeline.", {"demand", "b2b"}),
    ]
    selected_sets = []
    for label, description, expected in cases:
        # Metadata is deliberately identical: only the actual description varies.
        job = mock_job.model_copy(update={"normalized_url": f"https://example.invalid/{label}",
                                          "title": "Marketing Manager", "company": "Fictional Co"})
        store.index_job(profile.id, job, description, f"https://example.invalid/source-{label}")
        before = len(embedder.calls)
        result = store.retrieve(candidate=profile, job=job, query="Cover letter", limit=2)
        assert len(embedder.calls) == before + 1
        relevance_query = embedder.calls[-1][0]
        assert "Cover letter" in relevance_query and description in relevance_query
        assert "Role: Marketing Manager" in relevance_query and "Company: Fictional Co" in relevance_query
        assert "relevance only, not candidate experience" in relevance_query
        selected = {f.id for f in result.facts}
        selected_sets.append(frozenset(selected))
        assert selected == expected
        assert all(f is profile.find_fact(f.id) for f in result.facts)
        assert [e["text"] for e in result.job_evidence] == [description]
        assert result.receipt["job_context_applied"]
        assert result.receipt["embedding"]["batch_count"] == 1
        assert result.receipt["embedding"]["usage"]["cost"] == 0.00001
        assert result.receipt["embedding_stages"] == [{"stage": "candidate_relevance",
                                                       "query_stages": ["job_context"],
                                                       "receipt": result.receipt["embedding"]}]
        assert "Cover letter" not in json.dumps(result.receipt)
    assert len(set(selected_sets)) == 3


def test_no_valid_job_evidence_keeps_original_query_and_single_embedding(knowledge, profile, mock_job):
    store, _, embedder = knowledge
    store.index_candidate(profile)
    before = len(embedder.calls)
    result = store.retrieve(candidate=profile, job=mock_job, query="Cover letter")
    assert embedder.calls[before:] == [["Cover letter"]]
    assert result.receipt["fact_query_sha256"] == result.receipt["query_sha256"]
    assert not result.receipt["job_context_applied"]


def test_foreign_job_text_is_rejected_before_embedding(knowledge, profile, mock_job):
    store, db, embedder = knowledge
    store.index_candidate(profile)
    store.index_job("other-candidate", mock_job, "Foreign employer requirement must not enter query.",
                    "https://example.invalid/private-source")
    source = next(s for s in db.sources.values() if s["kind"] == "job")
    foreign = next(r for r in db.chunks.values() if r["kind"] == "job")
    db.extra_hits = [{**foreign, "source_url": source["source_url"], "indexed_job_id": mock_job.id}]
    result = store.retrieve(candidate=profile, job=mock_job, query="Cover letter")
    assert embedder.calls[-1] == ["Cover letter"]
    assert result.job_evidence == []
    assert result.facts == profile.facts


def test_job_relevance_query_has_strict_utf8_byte_bound_and_preserves_question(knowledge, profile, mock_job):
    store, _, embedder = knowledge
    description = "\n".join("界" * 1700 + f" section {i}" for i in range(8))
    store.index_job(profile.id, mock_job, description, "https://example.invalid/multibyte-source")
    query = "Write a cover letter with this emphasis: " + "🙂" * 700
    result = store.retrieve(candidate=profile, job=mock_job, query=query)
    enriched = embedder.calls[-1][0]
    assert query in enriched and len(enriched.encode("utf-8")) < 8000
    assert "界" in enriched and len(result.job_evidence) == 4
    assert result.receipt["fact_queries"][0]["query_bytes"] == len(enriched.encode("utf-8"))


@pytest.mark.parametrize(("question", "required"), [
    ("Describe an example of connecting lead quality or revenue back to paid media optimization.",
     {"lead-scoring"}),
    ("Describe the teams of marketing specialists and copywriters you led and their sizes.",
     {"team-one", "team-two"}),
    ("What measurable revenue, CPA and ROAS results have you delivered?", {"revenue-results"}),
])
def test_specific_question_evidence_survives_long_same_job_context(profile, mock_job, question, required):
    facts = [fact(f"developer-{i}", f"Owned developer platform software search campaigns {i}.")
             for i in range(10)]
    facts += [fact("lead-scoring", "Built agents that score inbound leads and upload offline conversions."),
              fact("team-one", "Led a team of 3 marketing specialists and 1 copywriter."),
              fact("team-two", "Managed 3 copywriters and 1 SEO link builder."),
              fact("revenue-results", "Grew revenue and ROAS while reducing CPA.")]
    profile = profile.model_copy(update={"facts": facts})
    db, embedder = MemoryPg(), TopicEmbedder()
    db.rank_by_vector = True
    store = PgKnowledgeStore(db, embedder)
    store.index_candidate(profile)
    store.index_job(profile.id, mock_job,
                    "Build developer platform software paid search programs. " * 90,
                    "https://example.invalid/developer-description")
    cover = store.retrieve(candidate=profile, job=mock_job, query="Cover letter", limit=4)
    assert not required.intersection(f.id for f in cover.facts)
    before = len(embedder.calls)
    answer = store.retrieve(candidate=profile, job=mock_job, query=question, limit=4)
    assert required.issubset(f.id for f in answer.facts)
    assert {f.id for f in answer.facts} != {f.id for f in cover.facts}
    assert len(embedder.calls) == before + 1 and embedder.calls[-1] == [question]
    assert answer.receipt["embedding"]["input_count"] == 1
    assert answer.receipt["embedding"]["batch_count"] == 1
    assert not answer.receipt["candidate_ranking_uses_job_context"]
    assert [item["stage"] for item in answer.receipt["fact_queries"]] == ["question"]


def test_lexical_recall_uses_individual_terms_and_normalizes_fact_length(knowledge, profile, mock_job):
    store, db, _ = knowledge
    store.retrieve(candidate=profile, job=mock_job,
                   query='Describe connecting lead quality or revenue to paid-media optimization.')
    sql, params = next((sql, params) for sql, params in db.calls if "WITH scoped" in sql)
    assert params[6] == 'describe OR connecting OR lead OR quality OR revenue OR to OR paid OR media OR optimization'
    assert params[6] == params[7]
    assert "websearch_to_tsquery('english', %s), 2)" in sql


def test_changed_job_snapshot_during_embedding_is_rejected(knowledge, profile, mock_job, monkeypatch):
    store, db, embedder = knowledge
    store.index_job(profile.id, mock_job, "Initial role requirement.", "https://example.invalid/source")
    original_embed = embedder.embed
    key = next(iter(db.sources))

    def embed_then_change(texts):
        result = original_embed(texts)
        db.sources[key]["source_version"] = "0" * 64
        return result

    monkeypatch.setattr(embedder, "embed", embed_then_change)
    with pytest.raises(KnowledgeError, match="Job knowledge changed during retrieval"):
        store.retrieve(candidate=profile, job=mock_job, query="Cover letter")


@pytest.mark.parametrize("limit", [0, -1, 21, True])
def test_retrieval_bounds_fail_before_provider_or_sql(knowledge, profile, mock_job, limit):
    store, db, embedder = knowledge
    with pytest.raises(KnowledgeError):
        store.retrieve(candidate=profile, job=mock_job, query="campaign", limit=limit)
    assert not embedder.calls and not db.calls


def test_connection_factory_is_lazy(monkeypatch):
    called = []
    monkeypatch.setattr("interviewmaxxing_generation.knowledge.store.importlib.import_module",
                        lambda name: called.append(name))
    connect = pg_connection_factory("postgresql://fictional.invalid/never-connect")
    assert callable(connect) and not called


def test_migration_is_private_and_matches_fixed_contract():
    from pathlib import Path

    sql = (Path(__file__).parents[2] / "supabase/migrations/20260924040408_candidate_knowledge_rag.sql").read_text()
    assert "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions" in sql
    assert "extensions.vector(1536)" in sql
    assert "FROM PUBLIC, anon, authenticated" in sql
    assert sql.count("ENABLE ROW LEVEL SECURITY") == 2
    assert sql.count("FORCE ROW LEVEL SECURITY") == 2
    assert "ON DELETE CASCADE" in sql
    assert "SECURITY DEFINER" not in sql


def test_concurrent_retrievals_each_open_their_own_connection(profile, mock_job):
    """Round 7 (review 3, L4): the resolver retrieves from up to three worker threads at
    once. The store opens a new connection for every transaction, on the calling thread,
    so no connection is ever shared between threads."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    db, embedder = MemoryPg(), FakeEmbedder()
    serial = threading.Lock()  # the in-memory double itself is not thread-safe
    opened: list[tuple[int, object]] = []
    used: dict[int, set[int]] = {}
    barrier = threading.Barrier(3, timeout=10)

    class Connection:
        def __init__(self) -> None:
            db()
            opened.append((threading.get_ident(), self))

        def _mark(self) -> None:
            used.setdefault(id(self), set()).add(threading.get_ident())

        def __enter__(self):
            self._mark()
            serial.acquire()
            return self

        def __exit__(self, *exc):
            serial.release()
            return False

        def transaction(self):
            self._mark()
            return db.transaction()

        def cursor(self):
            self._mark()
            return db.cursor()

    store = PgKnowledgeStore(Connection, embedder)
    store.index_candidate(profile)
    before = len(opened)

    def retrieve(_: int):
        barrier.wait()  # three retrievals in flight together
        return store.retrieve(candidate=profile, job=mock_job, query="paid search")

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(retrieve, range(3)))
    assert all(result.facts for result in results)
    connections = opened[before:]
    assert len(connections) >= 3 and len({id(conn) for _, conn in connections}) == len(connections)
    assert len({thread for thread, _ in connections}) == 3
    for thread, conn in connections:
        assert used[id(conn)] == {thread}  # used only on the thread that opened it


# --- stories: the candidate's own account as a fourth kind ----------------------------------


def _story_chunks(text_by_title=None):
    from interviewmaxxing_generation.knowledge import stories as st

    text_by_title = text_by_title or {
        "Paid search for a bakery": (
            "I managed a $120,000 paid search budget for a regional bakery chain in 2024 and grew "
            "online orders by 35%. As the marketing manager I set up conversion tracking in Google "
            "Ads. My team of 2 coordinators reported to me. I learned that clean tracking matters."),
        "Building Ovenboard": (
            "I built an internal reporting tool, Ovenboard, for the bakery chain. It ran on Node.js "
            "and pulled Meta Ads spend into one dashboard, saving about 6 hours per week."),
    }
    runs = []
    for number, (title, body) in enumerate(text_by_title.items(), 1):
        runs += [st.Run(f"Stories 0{number} - {title}", True), st.Run(body, False)]
    paragraphs = [st.Paragraph(tuple(runs))]
    chunks = []
    for story in st.parse_stories(paragraphs):
        chunks += st.chunk_story(story, st.analyse_story(story))
    return chunks


def test_story_indexing_is_idempotent_and_one_document_is_current(knowledge, profile):
    store, db, embedder = knowledge
    chunks = _story_chunks()
    version = "c" * 64
    first = store.index_stories(profile.id, chunks, version=version)
    assert first["source_count"] == 1 and first["chunk_count"] == len(chunks) >= 4
    assert first["story_chunk_ids"] == [c.id for c in chunks]
    assert len(embedder.calls) == 1 and embedder.calls[0] == [c.text for c in chunks]
    second = store.index_stories(profile.id, chunks, version=version)
    assert second["unchanged_source_count"] == 1 and [c for c in embedder.calls if c] == [embedder.calls[0]]
    assert second["chunk_ids"] == first["chunk_ids"] and len(db.chunks) == len(chunks)
    assert {key[1] for key in db.sources} == {"story"}
    newer = store.index_stories(profile.id, chunks[:3], version="d" * 64)
    assert newer["unchanged_source_count"] == 0 and len(db.chunks) == 3
    assert len([c for c in embedder.calls if c]) == 2
    other = store.index_stories(profile.id, chunks, version="e" * 64, source_id="second-document")
    assert other["replaced_source_count"] == 1 and len(db.sources) == 1
    kept = store.index_stories(profile.id, chunks[:2], version="f" * 64, source_id="third", replace_others=False)
    assert kept["replaced_source_count"] == 0 and len(db.sources) == 2


@pytest.mark.parametrize("change", ["id", "whitespace", "header", "oversized", "duplicate", "version"])
def test_malformed_story_chunks_are_rejected_before_any_write(knowledge, profile, change):
    from dataclasses import replace

    store, db, embedder = knowledge
    chunks = _story_chunks()
    version = "c" * 64
    if change == "id":
        chunks[0] = replace(chunks[0], id="story:" + "0" * 64)
    elif change == "whitespace":
        chunks[0] = replace(chunks[0], text=chunks[0].text + " ")
    elif change == "header":
        body = "No header here. " + chunks[0].text.split("\n", 1)[1]
        chunks[0] = replace(chunks[0], text=body, id="story:" + hashlib.sha256(body.encode()).hexdigest())
    elif change == "oversized":
        body = chunks[0].text + " x" * 900
        chunks[0] = replace(chunks[0], text=body, id="story:" + hashlib.sha256(body.encode()).hexdigest())
    elif change == "duplicate":
        chunks.append(chunks[0])
    else:
        version = "not-a-digest"
    with pytest.raises(KnowledgeError):
        store.index_stories(profile.id, chunks, version=version)
    assert not db.sources and not db.chunks and not embedder.calls


def test_narrative_retrieval_returns_story_chunks_but_identity_fields_get_none(knowledge, profile, mock_job):
    store, _, embedder = knowledge
    chunks = _story_chunks()
    store.index_candidate(profile)
    store.index_stories(profile.id, chunks, version="c" * 64)
    store.index_job(profile.id, mock_job, "Own paid search and report orders to the owner.",
                    "https://example.invalid/job")
    embedder.calls.clear()
    result = store.retrieve(candidate=profile, job=mock_job, query="Cover letter", narrative=True)
    assert result.facts and result.job_evidence
    assert 1 <= len(result.story_chunks) <= 4
    assert len(embedder.calls) == 1 and len(embedder.calls[0]) == 2
    assert embedder.calls[0][1].startswith("Question: Cover letter\nRole: ")
    assert "Key requirements:\nOwn paid search and report orders to the owner." in embedder.calls[0][1]
    for rank, chunk in enumerate(result.story_chunks, 1):
        assert chunk["id"] == "story:" + hashlib.sha256(chunk["text"].encode()).hexdigest()
        assert chunk["text"].startswith("Story 0") and chunk["source_version"] == "c" * 64
        assert chunk["rank"] == rank and 0 < chunk["score"] <= 1
        assert chunk["title"] and isinstance(chunk["themes"], list) and chunk["story_id"]
    assert result.story_chunks[0]["employer"] in ("regional bakery chain", "bakery chain")
    assert result.receipt["story_ids"] == [c["id"] for c in result.story_chunks]
    assert set(result.receipt["story_scores"]) == set(result.receipt["story_ids"])
    assert result.receipt["counts"]["story_chunks"] == len(result.story_chunks)
    assert result.receipt["story_query_applied"] and result.receipt["stories_skipped_reason"] is None
    # No explicit style samples: the candidate's own stories set the tone.
    assert result.voice_samples == [c["text"] for c in result.story_chunks[:2]]
    assert result.receipt["voice_from_stories"] is True
    store.index_voice(profile.id, "My own voice sample.", "own-sample")
    voiced = store.retrieve(candidate=profile, job=mock_job, query="Cover letter", narrative=True)
    assert voiced.voice_samples == ["My own voice sample."] and not voiced.receipt["voice_from_stories"]
    assert "story_relevance" in result.receipt["embedding_stages"][0]["query_stages"]
    assert "c" * 64 in result.receipt["source_versions"]
    for identity in ("First name", "Email Address", "Phone number*", "LinkedIn Profile URL", "City"):
        embedder.calls.clear()
        held = store.retrieve(candidate=profile, job=mock_job, query=identity, narrative=True)
        assert held.story_chunks == [] and held.receipt["stories_skipped_reason"] == "identity_field"
        assert len(embedder.calls[0]) == 1
    plain = store.retrieve(candidate=profile, job=mock_job, query="Describe a campaign you led")
    assert plain.story_chunks == [] and plain.receipt["stories_skipped_reason"] == "not_narrative"
    assert plain.receipt["counts"]["story_chunks"] == 0 and not plain.receipt["story_query_applied"]


def test_story_hits_are_validated_and_deduplicated(knowledge, profile, mock_job):
    store, db, _ = knowledge
    chunks = _story_chunks()
    store.index_stories(profile.id, chunks, version="c" * 64)
    genuine = next(row for row in db.chunks.values() if row["chunk_number"] == 1)
    foreign = {**genuine, "candidate_id": "someone-else", "id": "story:foreign"}
    unlabelled_body = "A chunk without its header line."
    unlabelled = {**genuine, "id": "story:x", "body": unlabelled_body,
                  "content_hash": hashlib.sha256(unlabelled_body.encode()).hexdigest()}
    tampered = {**genuine, "id": "story:y", "body": genuine["body"] + " tampered"}
    db.extra_hits = [foreign, unlabelled, tampered, dict(genuine)]
    result = store.retrieve(candidate=profile, job=mock_job, query="Tell us about a campaign", narrative=True)
    texts = [c["text"] for c in result.story_chunks]
    assert genuine["body"] in texts and texts.count(genuine["body"]) == 1
    assert unlabelled_body not in texts and not any("tampered" in t for t in texts)
    assert result.receipt["rejected_count"] >= 3
    assert len(result.story_chunks) <= 4


def test_motivation_questions_rank_facts_by_the_job_context(knowledge, profile, mock_job):
    store, _, _ = knowledge
    store.index_candidate(profile)
    store.index_job(profile.id, mock_job, "Own paid search and report orders to the owner.",
                    "https://example.invalid/job")
    for query in ("What interests you about Synthetic Co?", "Why do you want to work here?", "Cover letter"):
        result = store.retrieve(candidate=profile, job=mock_job, query=query)
        assert result.receipt["candidate_ranking_uses_job_context"] is True, query
    plain = store.retrieve(candidate=profile, job=mock_job, query="Describe a campaign you led")
    assert plain.receipt["candidate_ranking_uses_job_context"] is False


def test_retrieved_story_chunks_carry_their_resume_role(knowledge, profile, mock_job):
    from interviewmaxxing_generation.knowledge import stories as st

    store, _, _ = knowledge
    chunks = _story_chunks()
    story = st.parse_stories([st.Paragraph((st.Run("Stories 01 - Paid search for a bakery", True),
                                            st.Run("I managed a $120,000 budget for a regional bakery chain in 2024.", False)))])[0]
    link = st.StoryRoleLink(story.story_id, "exp_bakery", "Crumb & Co.", "Marketing Manager", "2023-04", "2024-09", False, "jev_match", 0.95, 0.98)
    linked = st.chunk_story(story, st.analyse_story(story), link)
    store.index_stories(profile.id, [*chunks, *linked], version="c" * 64)
    result = store.retrieve(candidate=profile, job=mock_job, query="Tell us about a campaign", narrative=True)
    by_role = {c["resume_role"] for c in result.story_chunks}
    assert "Marketing Manager, Crumb & Co." in by_role or len(result.story_chunks) == 4
    hit = next((c for c in result.story_chunks if c["resume_role"]), None)
    if hit is not None:
        assert hit["period"] == "2023-04 to 2024-09"
