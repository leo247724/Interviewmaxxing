"""Private PostgreSQL projection, never an authority for candidate facts.

The connection factory must return an owned psycopg3 connection/context manager.
Queries are parameterized and candidate-scoped even when a privileged connection
bypasses RLS. The caller refreshes job descriptions from its trusted listing store.
Job scope is the conservatively normalized application URL, so discovery/runtime
record IDs can differ without matching by employer, title, or a redirect.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

from interviewmaxxing_core import CandidateFact, CandidateProfile, JobRecord
from interviewmaxxing_core.urls import normalize_application_url

from ..questions import motivation_question
from .embeddings import (
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL,
    MAX_EMBEDDING_INPUTS,
    MAX_INPUT_BYTES,
    Embedder,
    EmbeddingError,
    EmbeddingResult,
    KnowledgeError,
    validate_vector,
)
from .stories import DEFAULT_STORY_SOURCE, StoryChunk, parse_chunk_header

MAX_RETRIEVAL_LIMIT = 20
MAX_SOURCE_CHARS = 100_000
CHUNK_CHARS = 1800
MAX_SOURCE_CHUNKS = 64
MAX_STORY_CHUNKS = 4
"""Story chunks returned for one narrative retrieval (each about 120-300 words)."""
MAX_REQUIREMENT_CHARS = 1500
MAX_JOB_CONTEXT_CHUNKS = 5
"""A cover letter or motivation answer gets the whole job description up to this many
chunks, else this many chunks ranked by requirement cues (round 6)."""
MAX_REQUIREMENT_QUERIES = 10
"""Key requirements a cover letter or motivation answer retrieves facts for, each its own
embedding input in the one request and its own hybrid query."""
PRIORITY_STORY_CHUNKS = 2
"""Story chunks ranked first for a cover letter or motivation answer by the posting's first
priority alone: the letter's proof is drawn from the passage that best matches it."""
FACTS_PER_REQUIREMENT = 2
REQUIREMENT_POOL = 3
"""The best hits per requirement among which a fact stating a figure is preferred; the
whole description's ranking prefers figures within windows of the same size, so a weak
match never jumps ahead of a strong one for its number alone."""


@dataclass(frozen=True)
class RetrievalResult:
    facts: list[CandidateFact] = field(default_factory=list)
    job_evidence: list[dict[str, str]] = field(default_factory=list)
    voice_samples: list[str] = field(default_factory=list)
    receipt: dict[str, Any] = field(default_factory=dict)
    story_chunks: list[dict[str, Any]] = field(default_factory=list)
    """Narrative evidence in the candidate's own words: ``id`` (``story:<content hash>``),
    ``text``, ``story_id``, ``title``, ``employer``, ``period``, ``themes``,
    ``source_version``, ``score`` and ``rank``. Empty unless ``narrative`` retrieval."""


class KnowledgeRetriever(Protocol):
    def retrieve(self, *, candidate: CandidateProfile, job: JobRecord,
                 query: str, limit: int = 8, narrative: bool = False) -> RetrievalResult: ...


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_hash(value: object) -> str:
    return _hash(json.dumps(value, sort_keys=True, ensure_ascii=False,
                            separators=(",", ":"), allow_nan=False))


def fact_fingerprint(fact: CandidateFact) -> str:
    """Binds ID, value, key, source, verification, confidence and source evidence."""
    return _json_hash(fact.model_dump(mode="json"))


def _normalized_url(value: str) -> str:
    try:
        return normalize_application_url(value)
    except ValueError:
        raise KnowledgeError("Knowledge source requires a valid HTTP(S) URL") from None


def job_fingerprint(job: JobRecord) -> str:
    """Exact URL boundary, independent of temporary record IDs and mutable labels."""
    if job.merged_into:
        raise KnowledgeError("Resolve the canonical job before indexing or retrieval")
    return _json_hash({"normalized_url": _normalized_url(job.normalized_url)})


def pg_connection_factory(dsn: str, *, connect_timeout: int = 10) -> Callable[[], Any]:
    """Lazy psycopg import; no DB connection or credential use at construction."""
    if not dsn.strip() or not 1 <= connect_timeout <= 30:
        raise ValueError("A DSN and bounded connection timeout are required")

    def connect() -> Any:
        try:
            psycopg = importlib.import_module("psycopg")
        except ImportError:
            raise KnowledgeError("Knowledge retrieval requires psycopg3") from None
        return psycopg.connect(dsn, connect_timeout=connect_timeout)

    return connect


def _chunks(text: str) -> list[str]:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_SOURCE_CHARS:
        raise KnowledgeError("Knowledge source text is empty or exceeds its size limit")
    remaining = text.strip()
    result: list[str] = []
    while remaining:
        boundary = min(CHUNK_CHARS, len(remaining))
        if boundary < len(remaining):
            preferred = max(remaining.rfind("\n", 0, boundary),
                            remaining.rfind(" ", 0, boundary))
            if preferred >= CHUNK_CHARS // 2:
                boundary = preferred
        result.append(remaining[:boundary].strip())
        remaining = remaining[boundary:].lstrip()
    if len(result) > MAX_SOURCE_CHUNKS:
        raise KnowledgeError("Knowledge source exceeds its chunk limit")
    return result


def _fact_text(fact: CandidateFact) -> str:
    value = fact.value if isinstance(fact.value, str) else json.dumps(
        fact.value, ensure_ascii=False, allow_nan=False)
    return "\n".join([f"{fact.key}: {value}", *fact.evidence])


@dataclass(frozen=True)
class _Source:
    kind: str
    job_scope: str
    source_id: str
    version: str
    chunks: list[str]
    source_url: str = ""
    indexed_job_id: str = ""


_RESULT_COLUMNS = (
    "id", "candidate_id", "kind", "job_scope", "source_id", "source_version",
    "chunk_number", "body", "content_hash", "source_url", "indexed_job_id",
)
_STORY_RESULT_COLUMNS = (*_RESULT_COLUMNS, "score")

_SNAPSHOT_SQL = """
SELECT s.source_id, s.source_version,
       array_agg(c.content_hash ORDER BY c.chunk_number) FILTER (WHERE c.id IS NOT NULL) AS chunk_hashes,
       array_agg(c.embedding_model ORDER BY c.chunk_number) FILTER (WHERE c.id IS NOT NULL) AS models
FROM imx_knowledge.sources s
LEFT JOIN imx_knowledge.chunks c
    USING (candidate_id, kind, job_scope, source_id, source_version)
WHERE s.candidate_id = %s AND s.kind = %s AND s.job_scope = %s
GROUP BY s.source_id, s.source_version
"""

SourceSignature = tuple[str, tuple[str, ...], tuple[str, ...]]


def _signature(source: _Source) -> SourceSignature:
    return (source.version, tuple(_hash(t) for t in source.chunks),
            (EMBEDDING_MODEL,) * len(source.chunks))


# One canonical source is selected before its chunks. This also prevents mixing
# snapshots if a legacy index predates the one-description-per-job write rule.
_JOB_CONTEXT_SQL = """
WITH current_job AS (
    SELECT * FROM imx_knowledge.sources
    WHERE candidate_id = %s AND kind = 'job' AND job_scope = %s
    ORDER BY indexed_at DESC, source_id LIMIT 1
)
SELECT c.id, c.candidate_id, c.kind, c.job_scope, c.source_id, c.source_version,
       c.chunk_number, c.body, c.content_hash, s.source_url, s.indexed_job_id
FROM current_job s
JOIN imx_knowledge.chunks c
    USING (candidate_id, kind, job_scope, source_id, source_version)
WHERE c.embedding_model = %s
ORDER BY ts_rank_cd(c.search_terms, websearch_to_tsquery('english', %s)) DESC,
         c.chunk_number, c.id
LIMIT %s
"""

# The whole current description, in order, for a cover letter or motivation answer: a
# generic question must not pick the job's chunks by its own words (round 6).
_JOB_SOURCE_SQL = """
WITH current_job AS (
    SELECT * FROM imx_knowledge.sources
    WHERE candidate_id = %s AND kind = 'job' AND job_scope = %s
    ORDER BY indexed_at DESC, source_id LIMIT 1
)
SELECT c.id, c.candidate_id, c.kind, c.job_scope, c.source_id, c.source_version,
       c.chunk_number, c.body, c.content_hash, s.source_url, s.indexed_job_id
FROM current_job s
JOIN imx_knowledge.chunks c
    USING (candidate_id, kind, job_scope, source_id, source_version)
WHERE c.embedding_model = %s
ORDER BY c.chunk_number, c.id
LIMIT %s
"""


def _fact_relevance_query(query: str, job: JobRecord, evidence: list[dict[str, str]]) -> str:
    if not evidence:
        return query
    # Job text influences ranking only; the returned candidate facts are still
    # separately loaded and compared with the current verified canonical objects.
    # Preserve the full actual question and give long/multibyte JDs a byte bound.
    combined = (f"Question: {query}\n"
                f"Job context for relevance only, not candidate experience:\n"
                f"Role: {job.title or ''}\nCompany: {job.company or ''}\n"
                + "\n".join(item["text"] for item in evidence))
    return combined.encode("utf-8")[:MAX_INPUT_BYTES - 1].decode("utf-8", errors="ignore")


def _cover_letter_request(query: str) -> bool:
    """A cover letter, or an interest, motivation or fit question (a cover-letter
    narrative): candidate facts are then ranked by the job's own description."""
    normalized = query.strip().lower()
    return bool(re.fullmatch(r"(?:optional\s+)?cover[\s-]*letter(?:\s*\(optional\))?[.:?!]*", normalized)
                or re.match(r"^(?:please\s+)?(?:write|draft|create|compose)\b.{0,80}\bcover[\s-]*letter\b",
                            normalized)
                or motivation_question(query))


_IDENTITY_QUERY = re.compile(
    r"^(?:your\s+)?(?:(?:first|last|full|preferred|given|family|legal)\s+)?names?$"
    r"|^(?:your\s+)?(?:e-?mail|email\s+address|e-?mail\s+address)$"
    r"|^(?:your\s+)?(?:mobile|cell|phone|telephone|contact)(?:\s+(?:phone\s+)?number)?$"
    r"|^(?:your\s+)?(?:linkedin|github|website|portfolio)(?:\s+(?:profile|url|link))*$"
    r"|^(?:your\s+)?(?:street\s+)?address(?:\s+line\s*\d)?$|^(?:city|state|region|province|country|"
    r"zip(?:\s*code)?|postal\s*code|location|date\s+of\s+birth|pronouns)$")
_REQUIREMENT_CUE = re.compile(
    r"\b(?:experience|years?|manage|managing|own|owning|lead|leading|build|building|drive|driving|"
    r"responsib|require|must|you will|you'll|you are|proven|track record|hands-on|expertise|"
    r"skills?|ability|strong|deep|familiar)\b", re.IGNORECASE)


def _identity_query(query: str) -> bool:
    """A bare identity or contact wording; such a field never gets story evidence."""
    normalized = re.sub(r"[\s*:?.!]+$", "", query.strip().lower())
    normalized = re.sub(r"\s*\((?:optional|required)\)$", "", normalized)
    return bool(_IDENTITY_QUERY.match(normalized))


_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+|(?<=[a-z])\s*[\u2022\u00b7\-\u2013]\s+(?=[A-Z])")


def _requirement_lines(evidence: list[dict[str, str]]) -> list[str]:
    """Requirement-like sentences of the scoped job description, in order, each once,
    bounded to ``MAX_REQUIREMENT_CHARS`` together."""
    lines: list[str] = []
    seen: set[str] = set()
    size = 0
    for item in evidence:
        for sentence in _SENTENCE_BREAK.split(item["text"]):
            sentence = re.sub(r"^[\u2022\u00b7\-\u2013*]+\s*", "", " ".join(sentence.split()))
            if len(sentence) < 20 or not _REQUIREMENT_CUE.search(sentence) or sentence.casefold() in seen:
                continue
            if size + len(sentence) + 1 > MAX_REQUIREMENT_CHARS:
                return lines
            lines.append(sentence)
            seen.add(sentence.casefold())
            size += len(sentence) + 1
    return lines


def _key_requirements(evidence: list[dict[str, str]]) -> str:
    """Requirement-like sentences of the scoped job description, bounded."""
    lines = _requirement_lines(evidence)
    if not lines:
        return "\n".join(item["text"] for item in evidence)[:MAX_REQUIREMENT_CHARS]
    return "\n".join(lines)


def _requirement_cues(text: str) -> int:
    """How many requirement-like sentences a job chunk holds: ranks a long description's
    chunks for a cover letter, so the qualifications beat the salary and EEO text."""
    return sum(1 for sentence in _SENTENCE_BREAK.split(text)
               if len(sentence.strip()) >= 20 and _REQUIREMENT_CUE.search(sentence))


_YEARS = re.compile(r"\b(\d{1,2})\s*(?:\+|plus\b)?\s*(?:(?:-|\u2013|to)\s*\d{1,2}\s*)?(?:years?|yrs)\b",
                    re.IGNORECASE)
_NUMBER = re.compile(r"(?<![A-Za-z\d])\d+(?:[.,]\d+)*(?![A-Za-z\d])|(?<![A-Za-z\d])\d+(?:[.,]\d+)*(?:[kKmMbBx])\b")
"""A figure: a number standing on its own or with a unit ("58%", "$400K", "8.5x"), not a digit
inside a name ("B2B", "Q4")."""
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_CLAIM_WORD = re.compile(r"[^\W\d_]{4,}")
_CLAIM_STOP = frozenset({"with", "from", "into", "that", "this", "their", "they", "them", "were",
                         "have", "while", "over", "across", "after", "before", "which", "about",
                         "resume", "years", "year"})
_CONTEXT_SUFFIX = re.compile(r"\s\([^()]*\)$")
"""The "(employer; resume: company, period)" a story fact's value ends with."""


def _fact_value_text(fact: CandidateFact) -> str:
    return fact.value if isinstance(fact.value, str) else json.dumps(
        fact.value, ensure_ascii=False, allow_nan=False)


def _years_count(fact: CandidateFact) -> int | None:
    """The years a years-of-experience fact states ("7 years of performance marketing",
    ``years_experience: 7``), or None: a fact whose only figure is a count of years. A
    result that spans years ("$400K a month over 3 years") states other figures too."""
    text = _CONTEXT_SUFFIX.sub("", _fact_value_text(fact))
    if isinstance(fact.value, (int, float)) and not isinstance(fact.value, bool):
        return int(fact.value) if "year" in fact.key.casefold() else None
    match = _YEARS.search(text)
    if match is None:
        return None
    rest = text[:match.start()] + " " + text[match.end():]
    return None if _NUMBER.search(rest) else int(match.group(1))


def _states_figure(fact: CandidateFact) -> bool:
    """A fact that states a result or a scale: a figure other than a years count or a
    calendar year."""
    if _years_count(fact) is not None:
        return False
    return any(not _YEAR.match(number) for number in _NUMBER.findall(_CONTEXT_SUFFIX.sub("", _fact_value_text(fact))))


def _claim_terms(fact: CandidateFact) -> tuple[frozenset[str], frozenset[str]]:
    text = _CONTEXT_SUFFIX.sub("", _fact_value_text(fact)).casefold()
    numbers = frozenset(number.replace(",", "") for number in _NUMBER.findall(text))
    words = frozenset(word for word in _CLAIM_WORD.findall(text) if word not in _CLAIM_STOP)
    return numbers, words


def same_claim(first: CandidateFact, second: CandidateFact) -> bool:
    """Whether two facts state the same claim, as a resume bullet and the story fact that
    retells it do: they share a figure and at least 40% of the shorter one's content words,
    or, without figures, at least 60% of them."""
    first_numbers, first_words = _claim_terms(first)
    second_numbers, second_words = _claim_terms(second)
    if not first_words or not second_words:
        return False
    overlap = len(first_words & second_words) / min(len(first_words), len(second_words))
    if first_numbers and second_numbers:
        return bool(first_numbers & second_numbers) and overlap >= 0.4
    return not first_numbers and not second_numbers and overlap >= 0.6


def _years_asks(requirements: list[str]) -> list[tuple[int, frozenset[str]]]:
    """The posting's stated years asks ("10+ years of growth marketing"), each with the
    content words of its requirement, the lower bound of a range."""
    asks = []
    for line in requirements:
        for match in _YEARS.finditer(line):
            words = frozenset(word for word in _CLAIM_WORD.findall(line.casefold()) if word not in _CLAIM_STOP)
            asks.append((int(match.group(1)), words))
    return asks


def _below_ask(fact: CandidateFact, years: int, asks: list[tuple[int, frozenset[str]]]) -> bool:
    """A years fact is below the posting's ask for the same thing: the ask whose
    requirement shares the most content words with the fact, else the largest ask."""
    if not asks:
        return False
    _, words = _claim_terms(fact)
    best = max(asks, key=lambda ask: (len(ask[1] & words), ask[0]))
    ask = best[0] if best[1] & words else max(years_asked for years_asked, _ in asks)
    return years < ask


def select_requirement_facts(pools: list[list[CandidateFact]], fill: list[CandidateFact], *,
                             requirements: list[str], limit: int,
                             ) -> tuple[list[CandidateFact], dict[str, Any]]:
    """Facts for a cover letter or motivation answer, per key requirement (round 6).

    ``pools`` holds each requirement's best hits in rank order; a fact stating a figure
    moves ahead within its requirement. Each requirement gets its top fact before any gets
    a second, up to ``FACTS_PER_REQUIREMENT``; then ``fill`` (the whole description's
    ranking) tops the list up to ``limit``. A fact stating the same claim as one already
    chosen is left out, as is a second years-of-experience fact and any years fact below
    the posting's stated ask for the same thing. Returns the facts and a receipt of ids."""
    asks = _years_asks(requirements)
    chosen: list[CandidateFact] = []
    picked: list[list[str]] = [[] for _ in pools]
    skipped: dict[str, list[str]] = {"same_claim": [], "years_below_ask": [], "second_years": []}
    years_chosen = False

    left_out: set[str] = set()

    def take(fact: CandidateFact) -> bool:
        """Choose the fact unless it is chosen already or left out: a fact left out once
        stays out under its first reason (chosen facts never leave)."""
        nonlocal years_chosen
        if fact.id in left_out or any(fact.id == other.id for other in chosen):
            return False
        years = _years_count(fact)
        reason = ("same_claim" if any(same_claim(fact, other) for other in chosen)
                  else None if years is None
                  else "years_below_ask" if _below_ask(fact, years, asks)
                  else "second_years" if years_chosen else None)
        if reason is not None:
            skipped[reason].append(fact.id)
            left_out.add(fact.id)
            return False
        years_chosen = years_chosen or years is not None
        chosen.append(fact)
        return True

    def figures_first(facts: list[CandidateFact]) -> list[CandidateFact]:
        return [fact for _, fact in sorted(enumerate(facts), key=lambda item: (
            item[0] // REQUIREMENT_POOL, not _states_figure(item[1]), item[0]))]

    ordered = [figures_first(pool) for pool in pools]
    for _ in range(FACTS_PER_REQUIREMENT):
        for index, pool in enumerate(ordered):
            if len(chosen) >= limit:
                break
            for fact in pool:
                if fact.id in picked[index]:
                    continue
                if take(fact):
                    picked[index].append(fact.id)
                    break
    for fact in figures_first(fill):
        if len(chosen) >= limit:
            break
        take(fact)
    return chosen, {"mode": "per_requirement", "requirements": len(pools), "limit": limit,
                    "per_requirement_ids": picked, "skipped": skipped,
                    "years_asks": sorted({years for years, _ in asks})}


def _story_relevance_query(query: str, job: JobRecord, evidence: list[dict[str, str]]) -> str:
    """Rank stories by the question, the job title and the description's key requirements."""
    combined = (f"Question: {query}\nRole: {job.title or ''}\nCompany: {job.company or ''}\n"
                "Key requirements:\n" + _key_requirements(evidence))
    return combined.encode("utf-8")[:MAX_INPUT_BYTES - 1].decode("utf-8", errors="ignore")


def _priority_story_query(job: JobRecord, requirement: str) -> str:
    """Rank stories by the posting's first priority alone (round 6 addendum: the story is
    the letter's spine, so its best match for that priority is listed first)."""
    combined = f"Role: {job.title or ''}\nFirst priority: {requirement}"
    return combined.encode("utf-8")[:MAX_INPUT_BYTES - 1].decode("utf-8", errors="ignore")


def _lexical_query(query: str) -> str:
    """Recall individual terms instead of requiring every word of a question.

    Alphanumeric terms cannot inject full-text operators; SQL still receives a
    bound parameter. PostgreSQL handles stemming and language stop words.
    """
    terms = dict.fromkeys(word.casefold() for word in re.findall(r"[^\W_]+", query)
                          if len(word) <= 80 and word.casefold() not in {"and", "or", "not"})
    return " OR ".join(list(terms)[:96])


def _valid_hit_body(hit: dict[str, Any]) -> bool:
    body, version = hit.get("body"), hit.get("source_version")
    return (isinstance(body, str) and bool(body.strip()) and len(body) <= CHUNK_CHARS
            and hit.get("content_hash") == _hash(body)
            and isinstance(version, str) and len(version) == 64)

# RRF combines bounded lexical and semantic lists; each list is filtered before
# ranking. Exact vector distances avoid ANN's under-return under tenant filters.
_RETRIEVE_SQL = """
WITH scoped AS NOT MATERIALIZED (
    SELECT c.*, s.source_url, s.indexed_job_id
    FROM imx_knowledge.chunks c
    JOIN imx_knowledge.sources s USING (candidate_id, kind, job_scope, source_id)
    WHERE c.candidate_id = %s AND c.kind = %s AND c.job_scope = %s
      AND c.source_version = s.source_version AND c.embedding_model = %s
), semantic AS (
    SELECT id, row_number() OVER (ORDER BY distance, id) AS rank
    FROM (SELECT id, embedding OPERATOR(extensions.<=>) %s::extensions.vector AS distance
          FROM scoped ORDER BY distance, id LIMIT %s) ranked
), lexical AS (
    SELECT id, row_number() OVER (ORDER BY score DESC, id) AS rank
    FROM (SELECT id, ts_rank_cd(search_terms, websearch_to_tsquery('english', %s), 2) AS score
          FROM scoped WHERE search_terms @@ websearch_to_tsquery('english', %s)
          ORDER BY score DESC, id LIMIT %s) ranked
)
SELECT d.id, d.candidate_id, d.kind, d.job_scope, d.source_id, d.source_version,
       d.chunk_number, d.body, d.content_hash, d.source_url, d.indexed_job_id
FROM scoped d
LEFT JOIN semantic v USING (id) LEFT JOIN lexical l USING (id)
WHERE v.id IS NOT NULL OR l.id IS NOT NULL
ORDER BY (coalesce(1.0 / (60 + v.rank), 0) + coalesce(1.0 / (60 + l.rank), 0)) DESC, d.id
LIMIT %s
"""

# The same hybrid ranking, returning its reciprocal-rank-fusion score for story traces.
_RETRIEVE_SCORED_SQL = _RETRIEVE_SQL.replace(
    "d.chunk_number, d.body, d.content_hash, d.source_url, d.indexed_job_id\n",
    "d.chunk_number, d.body, d.content_hash, d.source_url, d.indexed_job_id,\n"
    "       (coalesce(1.0 / (60 + v.rank), 0) + coalesce(1.0 / (60 + l.rank), 0)) AS score\n")
assert _RETRIEVE_SCORED_SQL != _RETRIEVE_SQL


class PgKnowledgeStore:
    def __init__(self, connection_factory: Callable[[], Any], embedder: Embedder) -> None:
        self._connection_factory = connection_factory
        self._embedder = embedder

    @contextmanager
    def _transaction(self, candidate_id: str, *, write: bool = False) -> Iterator[Any]:
        if not candidate_id.strip():
            raise KnowledgeError("A candidate ID is required")
        try:
            with self._connection_factory() as conn, conn.transaction(), conn.cursor() as cursor:
                cursor.execute("SELECT set_config('imx_knowledge.candidate_id', %s, true)",
                               (candidate_id,))
                cursor.execute("SET LOCAL statement_timeout = '15000ms'")
                cursor.execute("SET LOCAL lock_timeout = '5000ms'")
                if write:
                    cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                                   ("imx_knowledge:" + candidate_id,))
                yield cursor
        except KnowledgeError:
            raise
        except Exception:
            # SQL/DSN diagnostics may contain source text or credentials.
            raise KnowledgeError("Knowledge database operation failed") from None

    def _embed(self, texts: list[str]) -> EmbeddingResult:
        if len(texts) > MAX_EMBEDDING_INPUTS:
            raise KnowledgeError("Knowledge operation exceeds its embedding input limit")
        result = self._embedder.embed(texts)
        if (result.receipt.get("model") != EMBEDDING_MODEL
                or result.receipt.get("dimensions") != EMBEDDING_DIMENSIONS):
            raise EmbeddingError("Embedding contract metadata mismatch")
        if len(result.vectors) != len(texts):
            raise EmbeddingError("Embedding response count mismatch")
        return EmbeddingResult([validate_vector(v) for v in result.vectors], result.receipt)

    def index_candidate(self, candidate: CandidateProfile) -> dict[str, Any]:
        """Replace the verified projection; delete any facts now revoked/removed."""
        sources = [_Source("fact", "", fact.id, fact_fingerprint(fact),
                           _chunks(_fact_text(fact))) for fact in candidate.verified_facts()]
        return self._index(candidate.id, sources, revoke_facts=True)

    def index_job(self, candidate_id: str, job: JobRecord, description: str,
                  source_url: str) -> dict[str, Any]:
        """Replace the one current job snapshot, including when its source URL changes."""
        scope = job_fingerprint(job)
        url = _normalized_url(source_url)
        source = _Source("job", scope, _hash(url),
                         _json_hash([scope, url, description]), _chunks(description),
                         url, job.id)
        return self._index(candidate_id, [source])

    def index_voice(self, candidate_id: str, text: str, source_id: str) -> dict[str, Any]:
        """Explicitly supplied writing samples; returned only in the style channel."""
        if not source_id.strip():
            raise KnowledgeError("A voice source ID is required")
        source = _Source("voice", "", source_id, _json_hash([source_id, text]), _chunks(text))
        return self._index(candidate_id, [source])

    def index_stories(self, candidate_id: str, chunks: Sequence[StoryChunk], *,
                      version: str, source_id: str = DEFAULT_STORY_SOURCE,
                      replace_others: bool = True) -> dict[str, Any]:
        """Index one document's story chunks as the candidate's current stories source.

        ``version`` is the document's content hash; an unchanged document needs no new
        embeddings, a changed one replaces the source, and by default any other stories
        source of the candidate is removed so one document is current. Chunk ids are
        content hashes (``story:<sha256 of the chunk text>``) and are what retrieval
        returns, so fact provenance stays stable across re-indexing.
        """
        if not source_id.strip():
            raise KnowledgeError("A stories source ID is required")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
            raise KnowledgeError("A stories source version must be a SHA-256 hex digest")
        if not chunks or len(chunks) > MAX_SOURCE_CHUNKS:
            raise KnowledgeError("Story chunks are missing or exceed the chunk limit")
        seen: set[str] = set()
        for chunk in chunks:
            if (not isinstance(chunk, StoryChunk) or not chunk.text.strip()
                    or len(chunk.text) > CHUNK_CHARS or chunk.text != chunk.text.strip()
                    or chunk.id != "story:" + _hash(chunk.text) or chunk.id in seen
                    or parse_chunk_header(chunk.text) is None):
                raise KnowledgeError("A story chunk is malformed, oversized, duplicated or unlabelled")
            seen.add(chunk.id)
        source = _Source("story", "", source_id, version, [chunk.text for chunk in chunks])
        receipt = self._index(candidate_id, [source], replace_others=replace_others)
        receipt["story_chunk_ids"] = [chunk.id for chunk in chunks]
        return receipt

    def _index(self, candidate_id: str, sources: list[_Source], *,
               revoke_facts: bool = False, replace_others: bool = True) -> dict[str, Any]:
        started = time.monotonic()
        if sum(len(source.chunks) for source in sources) > MAX_EMBEDDING_INPUTS:
            raise KnowledgeError("Knowledge operation exceeds its chunk limit")
        kind = "fact" if revoke_facts else sources[0].kind
        scope = sources[0].job_scope if sources else ""
        with self._transaction(candidate_id) as cursor:
            existing = self._snapshot(cursor, candidate_id, kind, scope)
        unchanged = {source.source_id for source in sources
                     if existing.get(source.source_id) == _signature(source)}
        changed = [source for source in sources if source.source_id not in unchanged]
        embedded = self._embed([text for source in changed for text in source.chunks])
        offset, revoked_count, replaced_source_count = 0, 0, 0
        ids: list[str] = []
        with self._transaction(candidate_id, write=True) as cursor:
            if unchanged:
                locked = self._snapshot(cursor, candidate_id, kind, scope)
                if any(locked.get(source_id) != existing[source_id] for source_id in unchanged):
                    raise KnowledgeError("Knowledge index changed during preparation; retry indexing")
            if revoke_facts:
                cursor.execute("""DELETE FROM imx_knowledge.sources
                    WHERE candidate_id = %s AND kind = 'fact'
                      AND NOT (source_id = ANY(%s::text[]))""",
                               (candidate_id, [s.source_id for s in sources]))
                revoked_count = max(cursor.rowcount, 0)
            elif kind in ("job", "story") and replace_others:
                cursor.execute("""DELETE FROM imx_knowledge.sources
                    WHERE candidate_id = %s AND kind = %s AND job_scope = %s
                      AND source_id <> %s""", (candidate_id, kind, scope, sources[0].source_id))
                replaced_source_count = max(cursor.rowcount, 0)
            for source in sources:
                key = (candidate_id, source.kind, source.job_scope, source.source_id)
                rows = []
                for number, text in enumerate(source.chunks):
                    content_hash = _hash(text)
                    chunk_id = source.kind + ":" + _json_hash(
                        [*key, source.version, number, content_hash])
                    ids.append(chunk_id)
                    if source.source_id not in unchanged:
                        rows.append((chunk_id, *key, source.version, number, text, content_hash,
                                     json.dumps(embedded.vectors[offset]), EMBEDDING_MODEL))
                        offset += 1
                if source.source_id in unchanged:
                    continue
                cursor.execute("""DELETE FROM imx_knowledge.sources
                    WHERE candidate_id = %s AND kind = %s AND job_scope = %s AND source_id = %s""",
                               key)
                cursor.execute("""INSERT INTO imx_knowledge.sources
                    (candidate_id, kind, job_scope, source_id, source_version,
                     source_url, indexed_job_id) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                               (*key, source.version, source.source_url, source.indexed_job_id))
                cursor.executemany("""INSERT INTO imx_knowledge.chunks
                    (id, candidate_id, kind, job_scope, source_id, source_version,
                     chunk_number, body, content_hash, embedding, embedding_model)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::extensions.vector, %s)""",
                                   rows)
        return {"status": "indexed", "candidate_sha256": _hash(candidate_id),
                "source_count": len(sources), "chunk_count": len(ids), "chunk_ids": ids,
                "source_versions": sorted({s.version for s in sources}),
                "revoked_count": revoked_count, "replaced_source_count": replaced_source_count,
                "unchanged_source_count": len(unchanged), "embedding": embedded.receipt,
                "duration_ms": round((time.monotonic() - started) * 1000, 3)}

    @staticmethod
    def _snapshot(cursor: Any, candidate_id: str, kind: str,
                  scope: str) -> dict[str, SourceSignature]:
        cursor.execute(_SNAPSHOT_SQL, (candidate_id, kind, scope))
        result: dict[str, SourceSignature] = {}
        for raw in cursor.fetchall():
            row = dict(raw) if isinstance(raw, Mapping) else dict(zip(
                ("source_id", "source_version", "chunk_hashes", "models"), raw, strict=True))
            result[row["source_id"]] = (row["source_version"],
                                        tuple(row["chunk_hashes"] or ()), tuple(row["models"] or ()))
        return result

    @staticmethod
    def _scoped_hits(cursor: Any, candidate_id: str, kind: str, scope: str,
                     columns: tuple[str, ...] = _RESULT_COLUMNS) -> list[dict[str, Any]]:
        hits = []
        for raw in cursor.fetchall():
            row = dict(raw) if isinstance(raw, Mapping) else dict(zip(columns, raw, strict=True))
            # Do not let an incorrect connection, query, or injected row put
            # another candidate/job's text into either embeddings or output.
            if (row.get("candidate_id") != candidate_id or row.get("kind") != kind
                    or row.get("job_scope") != scope):
                hits.append({"invalid_scope": True})
            else:
                hits.append(row)
        return hits

    def _job_context(self, candidate_id: str, scope: str, query: str,
                     limit: int, *, whole: bool = False) -> tuple[list[dict[str, str]], int]:
        """The scoped job description's chunks: ranked by the question's own words for a
        specific question (at most ``min(limit, 4)``); for a cover letter or motivation
        answer (``whole``), the whole description when it has at most
        ``MAX_JOB_CONTEXT_CHUNKS`` chunks, else that many ranked by requirement cues, in
        the description's order."""
        with self._transaction(candidate_id) as cursor:
            if whole:
                cursor.execute(_JOB_SOURCE_SQL, (candidate_id, scope, EMBEDDING_MODEL, MAX_SOURCE_CHUNKS))
            else:
                cursor.execute(_JOB_CONTEXT_SQL, (candidate_id, scope, EMBEDDING_MODEL, _lexical_query(query),
                                                  min(limit * 4, 16)))
            hits = self._scoped_hits(cursor, candidate_id, "job", scope)
        jobs: list[dict[str, str]] = []
        seen: set[str] = set()
        rejected = 0
        bound = MAX_SOURCE_CHUNKS if whole else min(limit, 4)
        for hit in hits:
            url, identifier = hit.get("source_url"), hit.get("id")
            if (not _valid_hit_body(hit) or not isinstance(url, str) or not url
                    or not isinstance(identifier, str) or not identifier.startswith("job:")):
                rejected += 1
                continue
            try:
                if _normalized_url(url) != url or hit.get("source_id") != _hash(url):
                    rejected += 1
                    continue
            except KnowledgeError:
                rejected += 1
                continue
            if hit["content_hash"] not in seen and len(jobs) < bound:
                jobs.append({"id": identifier, "text": hit["body"],
                             "source_url": url, "source_version": hit["source_version"]})
                seen.add(hit["content_hash"])
        if whole and len(jobs) > MAX_JOB_CONTEXT_CHUNKS:
            ranked = sorted(range(len(jobs)), key=lambda index: (-_requirement_cues(jobs[index]["text"]), index))
            jobs = [jobs[index] for index in sorted(ranked[:MAX_JOB_CONTEXT_CHUNKS])]
        return jobs, rejected

    def retrieve(self, *, candidate: CandidateProfile, job: JobRecord,
                 query: str, limit: int = 8, narrative: bool = False) -> RetrievalResult:
        """Return up to limit facts, min(limit, 4) job chunks and two style chunks, plus
        up to four story chunks for a narrative field.

        Index hits must still match the supplied current canonical profile. A
        stale index therefore cannot restore a revoked or changed candidate claim.
        Job text supplies relevance for cover letters. Specific questions alone
        rank their candidate evidence, with the scoped JD returned separately for
        tailoring. Stories are ranked by the question, the job title and the
        description's key requirements, only when the caller marks the field as
        narrative (a WRITER-routed question or cover letter) and the question is not
        a bare identity field. Each retrieval uses one embedding request.

        A cover letter or motivation answer (round 6) gets the whole description (or its
        ``MAX_JOB_CONTEXT_CHUNKS`` chunks with the most requirement cues) and retrieves
        its facts per key requirement: each requirement is an input of the same embedding
        request and its own hybrid query, and ``select_requirement_facts`` picks one or two
        facts per requirement (figures first, no twin claims, at most one years fact and
        none below the posting's ask) before the description's overall ranking fills up to
        ``limit``.
        """
        if type(limit) is not int or not 1 <= limit <= MAX_RETRIEVAL_LIMIT:
            raise KnowledgeError("Retrieval limit must be between 1 and 20")
        if not isinstance(query, str) or not query.strip() or len(query) > CHUNK_CHARS:
            raise KnowledgeError("Retrieval query is empty or exceeds its size limit")
        started = time.monotonic()
        scope = job_fingerprint(job)
        letter = _cover_letter_request(query)
        jobs, rejected = self._job_context(candidate.id, scope, query, limit, whole=letter)
        job_context_ms = round((time.monotonic() - started) * 1000, 3)
        context_query = _fact_relevance_query(query, job, jobs)
        requirements: list[str] = []
        if jobs and letter:
            fact_queries = [("job_context", context_query, 1.0)]
            requirements = _requirement_lines(jobs)[:MAX_REQUIREMENT_QUERIES]
        else:
            fact_queries = [("question", query, 1.0)]
        identity_field = _identity_query(query)
        story_query = (_story_relevance_query(query, job, jobs)
                       if narrative and not identity_field else None)
        priority_query = _priority_story_query(job, requirements[0]) if story_query and requirements else None
        embedded = self._embed([text for _, text, _ in fact_queries]
                               + ([story_query] if story_query else [])
                               + ([priority_query] if priority_query else []) + requirements)
        requirement_offset = len(fact_queries) + (1 if story_query else 0) + (1 if priority_query else 0)
        pool_limit = min(limit * 4, 80)
        current = {f.id: f for f in candidate.verified_facts()}
        hits: list[dict[str, Any]] = []
        story_hits: list[dict[str, Any]] = []
        priority_hits: list[dict[str, Any]] = []
        requirement_hits: list[list[dict[str, Any]]] = []
        with self._transaction(candidate.id) as cursor:
            if jobs:
                current_sources = self._snapshot(cursor, candidate.id, "job", scope)
                for evidence in jobs:
                    current_source = current_sources.get(_hash(evidence["source_url"]))
                    if (not current_source or current_source[0] != evidence["source_version"]
                            or _hash(evidence["text"]) not in current_source[1]):
                        raise KnowledgeError("Job knowledge changed during retrieval; retry retrieval")
            lexical_query = _lexical_query(fact_queries[0][1])
            for kind in ("fact", "voice"):
                cursor.execute(_RETRIEVE_SQL, (candidate.id, kind, "", EMBEDDING_MODEL,
                                              json.dumps(embedded.vectors[0]), pool_limit,
                                              lexical_query, lexical_query,
                                              pool_limit, pool_limit))
                hits.extend(self._scoped_hits(cursor, candidate.id, kind, ""))
            if story_query:
                story_lexical = _lexical_query(story_query)
                cursor.execute(_RETRIEVE_SCORED_SQL, (candidate.id, "story", "", EMBEDDING_MODEL,
                                                     json.dumps(embedded.vectors[1]), pool_limit,
                                                     story_lexical, story_lexical,
                                                     pool_limit, pool_limit))
                story_hits = self._scoped_hits(cursor, candidate.id, "story", "",
                                               _STORY_RESULT_COLUMNS)
            if priority_query:
                priority_lexical = _lexical_query(priority_query)
                cursor.execute(_RETRIEVE_SCORED_SQL, (candidate.id, "story", "", EMBEDDING_MODEL,
                                                     json.dumps(embedded.vectors[len(fact_queries) + 1]),
                                                     pool_limit, priority_lexical, priority_lexical,
                                                     pool_limit, pool_limit))
                priority_hits = self._scoped_hits(cursor, candidate.id, "story", "", _STORY_RESULT_COLUMNS)
            for index, requirement in enumerate(requirements):
                requirement_lexical = _lexical_query(requirement)
                cursor.execute(_RETRIEVE_SQL, (candidate.id, "fact", "", EMBEDDING_MODEL,
                                              json.dumps(embedded.vectors[requirement_offset + index]),
                                              pool_limit, requirement_lexical, requirement_lexical,
                                              pool_limit, REQUIREMENT_POOL * 2))
                requirement_hits.append(self._scoped_hits(cursor, candidate.id, "fact", ""))

        def canonical_fact(hit: dict[str, Any]) -> CandidateFact | None:
            """The current canonical fact an index hit stands for, or None (counted as
            rejected) for a stale, revoked, changed or tampered hit."""
            nonlocal rejected
            source_id, number = hit.get("source_id"), hit.get("chunk_number")
            fact = current.get(source_id) if isinstance(source_id, str) else None
            if (not _valid_hit_body(hit) or fact is None or fact_fingerprint(fact) != hit["source_version"]
                    or type(number) is not int or number < 0):
                rejected += 1
                return None
            canonical_chunks = _chunks(_fact_text(fact))
            if number >= len(canonical_chunks) or hit["body"] != canonical_chunks[number]:
                rejected += 1
                return None
            return fact

        ranked_facts: list[CandidateFact] = []
        voices: list[str] = []
        stories: list[dict[str, Any]] = []
        seen_facts: set[str] = set()
        seen_voices: set[str] = set()
        seen_stories: set[str] = set()
        versions = {j["source_version"] for j in jobs}
        priority_ids: list[str] = []
        ranked_hits = ([(rank, hit, True) for rank, hit in enumerate(priority_hits, 1)]
                       + [(rank, hit, False) for rank, hit in enumerate(story_hits, 1)])
        for rank, hit, first_priority in ranked_hits:
            header = parse_chunk_header(hit["body"]) if _valid_hit_body(hit) else None
            if header is None:
                rejected += 1
                continue
            if (hit["content_hash"] in seen_stories or len(stories) >= MAX_STORY_CHUNKS
                    or (first_priority and len(priority_ids) >= PRIORITY_STORY_CHUNKS)):
                continue
            if first_priority:
                priority_ids.append("story:" + hit["content_hash"])
            score = hit.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
                score = 1.0 / (60 + rank)  # a driver without the fused score: its rank
            stories.append({"id": "story:" + hit["content_hash"], "text": hit["body"],
                            "story_id": header["story_id"], "title": header["title"],
                            "employer": header["employer"] or header["project"],
                            "resume_role": header["resume_role"],
                            "period": header["period"], "themes": list(header["themes"]),
                            "source_version": hit["source_version"], "score": float(score),
                            "rank": rank})
            seen_stories.add(hit["content_hash"])
            versions.add(hit["source_version"])
        voice_from_stories = False
        for hit in hits:
            if not _valid_hit_body(hit):
                rejected += 1
                continue
            body, version = hit["body"], hit["source_version"]
            kind = hit["kind"]
            if kind == "fact":
                fact = canonical_fact(hit)
                if fact is not None and fact.id not in seen_facts:
                    ranked_facts.append(fact)
                    seen_facts.add(fact.id)
            elif kind == "voice":
                if hit["content_hash"] not in seen_voices and len(voices) < 2:
                    voices.append(body)
                    seen_voices.add(hit["content_hash"])
                    versions.add(version)
        if not voices and stories:
            # The candidate's stories are their own writing: without explicit style
            # samples, the two best story chunks set the tone (style only, as ever).
            voices = [story["text"] for story in stories[:2]]
            voice_from_stories = True
        selection: dict[str, Any] = {"mode": "ranked", "limit": limit}
        if requirements:
            pools: list[list[CandidateFact]] = []
            for rows in requirement_hits:
                pool: list[CandidateFact] = []
                for hit in rows:
                    fact = canonical_fact(hit)
                    if fact is not None and len(pool) < REQUIREMENT_POOL and all(f.id != fact.id for f in pool):
                        pool.append(fact)
                pools.append(pool)
            facts, selection = select_requirement_facts(pools, ranked_facts, requirements=requirements,
                                                        limit=limit)
        else:
            facts = ranked_facts[:limit]
        versions.update(fact_fingerprint(fact) for fact in facts)

        return RetrievalResult(facts, jobs, voices, {
            "status": "retrieved", "backend": "pgvector-hybrid",
            "candidate_sha256": _hash(candidate.id), "job_scope_sha256": scope,
            "query_sha256": _hash(query), "fact_ids": [f.id for f in facts],
            "fact_query_sha256": _hash(fact_queries[0][1]),
            "fact_query_bytes": len(fact_queries[0][1].encode("utf-8")),
            "fact_queries": [{"stage": stage, "query_sha256": _hash(text),
                              "query_bytes": len(text.encode("utf-8")), "rank_weight": weight}
                             for stage, text, weight in [*fact_queries,
                                                         *(("requirement", line, 1.0) for line in requirements)]],
            "fact_selection": selection,
            "job_context_applied": bool(jobs), "job_context_duration_ms": job_context_ms,
            "candidate_ranking_uses_job_context": fact_queries[0][0] == "job_context",
            "job_evidence_ids": [j["id"] for j in jobs], "source_versions": sorted(versions),
            "story_ids": [s["id"] for s in stories],
            "story_scores": {s["id"]: s["score"] for s in stories},
            "story_query_applied": story_query is not None, "voice_from_stories": voice_from_stories,
            "story_priority_ids": priority_ids,
            "story_query_sha256": _hash(story_query) if story_query else None,
            "story_query_bytes": len(story_query.encode("utf-8")) if story_query else 0,
            "stories_skipped_reason": ("identity_field" if narrative and identity_field
                                       else None if narrative else "not_narrative"),
            "counts": {"facts": len(facts), "job_evidence": len(jobs), "voice_samples": len(voices),
                       "story_chunks": len(stories), "requirements": len(requirements)},
            "rejected_count": rejected, "embedding": embedded.receipt,
            "embedding_stages": [{"stage": "candidate_relevance",
                                   "query_stages": [stage for stage, _, _ in fact_queries]
                                   + (["story_relevance"] if story_query else [])
                                   + (["story_priority"] if priority_query else [])
                                   + (["requirement_relevance"] if requirements else []),
                                   "receipt": embedded.receipt}],
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
        }, stories)
