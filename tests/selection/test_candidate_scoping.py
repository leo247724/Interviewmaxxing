"""Every decision belongs to one candidate: cache, lookups, history and audit are scoped."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from interviewmaxxing_core import JobListing, SelectionChoice, SelectionPreferences
from interviewmaxxing_selection import (
    ApiKey,
    CandidateEvidence,
    JevClient,
    SelectionService,
    SelectionStore,
)

Listing = Callable[[str], JobListing]
MakeService = Callable[..., SelectionService]


@pytest.fixture
def twins(candidate: CandidateEvidence) -> tuple[CandidateEvidence, CandidateEvidence]:
    """Two candidates with byte-identical qualifications."""
    a = candidate.model_copy(update={"candidate_id": "cand_a"})
    b = candidate.model_copy(update={"candidate_id": "cand_b"})
    return a, b


def test_identical_qualifications_never_share_a_decision(
    listing: Listing,
    prefs: SelectionPreferences,
    twins: tuple[CandidateEvidence, CandidateEvidence],
    new_bot: Callable[..., Any],
    make_service: MakeService,
    store: SelectionStore,
) -> None:
    a, b = twins
    bot = new_bot()
    service = make_service(bot)
    item = listing("acquisition_lead")
    first = service.select(item, prefs, a)
    second = service.select(item, prefs, b)
    assert first.selection.candidate_id == "cand_a"
    assert second.selection.candidate_id == "cand_b"
    assert second.selection.id != first.selection.id
    assert len(bot.calls) == 4  # B was decided on its own, not served A's decision
    assert first.selection.candidate_evidence_hash == second.selection.candidate_evidence_hash
    assert first.cache_key != second.cache_key

    # Each candidate reuses only their own decision.
    assert service.select(item, prefs, a) == first
    assert service.select(item, prefs, b) == second
    assert len(bot.calls) == 4
    assert service.is_current(first.selection, item, prefs, a)
    assert not service.is_current(first.selection, item, prefs, b)
    assert not service.is_current(second.selection, item, prefs, a)

    # Store lookups are scoped too.
    assert store.latest(item.id, candidate_id="cand_a") == first
    assert store.latest(item.id, candidate_id="cand_b") == second
    assert store.latest(item.id, candidate_id="cand_c") is None
    assert [o.selection.id for o in store.history(item.id, candidate_id="cand_a")] == [first.selection.id]
    assert store.find_cached(item.id, "cand_b", first.cache_key) is None
    assert store.find_cached(item.id, "cand_a", first.cache_key) == first
    assert store.get(first.selection.id, candidate_id="cand_a") == first
    assert store.get(first.selection.id, candidate_id="cand_a") == first
    assert store.get(first.selection.id, candidate_id="cand_b") is None
    assert store.audit(first.selection.id, candidate_id="cand_b") is None
    audit = store.audit(first.selection.id, candidate_id="cand_a")
    assert audit is not None and audit.candidate_snapshot == a.model_dump(mode="json")


def test_no_profile_decisions_are_scoped_by_explicit_id(
    listing: Listing,
    prefs: SelectionPreferences,
    new_bot: Callable[..., Any],
    make_service: MakeService,
    store: SelectionStore,
) -> None:
    bot = new_bot()
    service = make_service(bot)
    item = listing("remote_manager")
    a = service.select(item, prefs, None, candidate_id="cand_a")
    b = service.select(item, prefs, None, candidate_id="cand_b")
    assert (a.selection.candidate_id, b.selection.candidate_id) == ("cand_a", "cand_b")
    assert a.selection.id != b.selection.id and len(bot.calls) == 4
    assert a.selection.effective_choice is SelectionChoice.REVIEW  # no profile: never APPLY
    assert service.select(item, prefs, None, candidate_id="cand_a") == a and len(bot.calls) == 4
    assert service.is_current(a.selection, item, prefs, None, candidate_id="cand_a")
    assert not service.is_current(a.selection, item, prefs, None, candidate_id="cand_b")
    assert store.latest(item.id, candidate_id="cand_b") == b


def test_candidate_id_resolution_is_explicit(
    listing: Listing,
    prefs: SelectionPreferences,
    candidate: CandidateEvidence,
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    item = listing("remote_manager")
    service = make_service(new_bot())
    with pytest.raises(ValueError, match="candidate_id is required"):
        service.select(item, prefs, None)
    with pytest.raises(ValueError, match="does not match"):
        service.select(item, prefs, candidate, candidate_id="someone_else")
    assert service.resolve_candidate_id(candidate) == candidate.candidate_id
    assert service.resolve_candidate_id(candidate, candidate.candidate_id) == candidate.candidate_id
    with_default = make_service(new_bot(), candidate_id="cand_default")
    assert with_default.select(item, prefs, None).selection.candidate_id == "cand_default"
    explicit = with_default.select(item, prefs, None, candidate_id="cand_x")
    assert explicit.selection.candidate_id == "cand_x"  # the call's id wins over the default
    # The evidence's own id is authoritative even when the service has a default.
    assert with_default.select(item, prefs, candidate).selection.candidate_id == (
        candidate.candidate_id
    )


def test_cache_key_includes_the_candidate(
    listing: Listing,
    prefs: SelectionPreferences,
    twins: tuple[CandidateEvidence, CandidateEvidence],
    new_bot: Callable[..., Any],
    make_service: MakeService,
) -> None:
    a, b = twins
    item = listing("remote_manager")
    service = make_service(new_bot())
    assert service.cache_key(item, prefs, a) != service.cache_key(item, prefs, b)
    assert service.cache_key(item, prefs, a) == service.cache_key(item, prefs, a)
    assert service.cache_key(item, prefs, None, candidate_id="x") != service.cache_key(
        item, prefs, None, candidate_id="y"
    )


OLD_SCHEMA = """
CREATE TABLE selection_decisions (
    id TEXT PRIMARY KEY,
    listing_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    cacheable INTEGER NOT NULL,
    effective_decision TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    record_json TEXT NOT NULL,
    job_snapshot_json TEXT NOT NULL,
    candidate_snapshot_json TEXT NOT NULL,
    preferences_json TEXT NOT NULL,
    requests_json TEXT NOT NULL,
    responses_json TEXT NOT NULL
);
CREATE INDEX selection_by_listing ON selection_decisions (listing_id, decided_at);
CREATE INDEX selection_by_cache ON selection_decisions (listing_id, cache_key);
"""


def test_store_migrates_rows_written_before_candidate_scoping(
    tmp_path: Path,
    listing: Listing,
    prefs: SelectionPreferences,
    candidate: CandidateEvidence,
    new_bot: Callable[..., Any],
    api_key: ApiKey,
) -> None:
    item = listing("remote_manager")
    client = JevClient(api_key, transport=new_bot(), sleep=lambda _s: None)
    out = SelectionService(client=client).select(item, prefs, candidate)  # no store
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO selection_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            out.selection.id,
            out.selection.listing_id,
            out.cache_key,
            1,
            out.selection.effective_choice.value,
            out.selection.decided_at.isoformat(),
            out.model_dump_json(),
            "{}",
            "null",
            "{}",
            "[]",
            "[]",
        ),
    )
    conn.commit()
    conn.close()

    store = SelectionStore(path)
    try:
        assert store.latest(item.id, candidate_id=candidate.candidate_id) == out
        assert store.latest(item.id, candidate_id="someone_else") is None
        assert store.find_cached(item.id, candidate.candidate_id, out.cache_key) == out
        assert store.get(out.selection.id, candidate_id=candidate.candidate_id) == out
        columns = {r[1] for r in store._conn.execute("PRAGMA table_info(selection_decisions)")}
        assert "candidate_id" in columns
    finally:
        store.close()
    SelectionStore(path).close()  # reopening an already migrated store is a no-op


def test_latest_many_is_scoped_chunked_and_deterministic(
    listing: Listing,
    prefs: SelectionPreferences,
    twins: tuple[CandidateEvidence, CandidateEvidence],
    new_bot: Callable[..., Any],
    make_service: MakeService,
    store: SelectionStore,
) -> None:
    from interviewmaxxing_core import utc_now

    a, b = twins
    service = make_service(new_bot(), clock=lambda: timestamp)
    timestamp = utc_now()
    item = listing("remote_manager")
    first = service.select(item, prefs, a)
    latest = service.select(item, prefs, a, use_cache=False)
    service.select(item, prefs, b)
    # Tied timestamps choose the later inserted row, never the other candidate.
    assert store.latest(item.id, candidate_id=a.candidate_id) == latest
    assert store.history(item.id, candidate_id=a.candidate_id) == [first, latest]
    queries: list[str] = []
    store._conn.set_trace_callback(queries.append)
    try:
        ids = [item.id, *[f"missing-{n}" for n in range(801)], item.id]
        assert store.latest_many(iter(ids), candidate_id=a.candidate_id) == {item.id: latest}
        assert len([q for q in queries if q.startswith("WITH requested")]) == 3
        queries.clear()
        assert store.latest_many([], candidate_id=a.candidate_id) == {}
        assert queries == []
    finally:
        store._conn.set_trace_callback(None)
    assert store.latest_many([item.id], candidate_id="absent") == {}


def test_nearby_thresholds_do_not_share_fingerprints(
    listing: Listing, prefs: SelectionPreferences, candidate: CandidateEvidence
) -> None:
    from interviewmaxxing_selection import SelectionPolicy

    a = SelectionService(client=None, policy=SelectionPolicy(apply_confidence=0.80000001))
    b = SelectionService(client=None, policy=SelectionPolicy(apply_confidence=0.80000002))
    item = listing("remote_manager")
    assert a.rubric_version != b.rubric_version
    assert a.cache_key(item, prefs, candidate) != b.cache_key(item, prefs, candidate)
    decision = a.select(item, prefs, candidate).selection
    assert not b.is_current(decision, item, prefs, candidate)


def test_concurrent_first_open_serializes_legacy_schema_inspection(tmp_path: Path) -> None:
    path = tmp_path / "legacy-concurrent.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(OLD_SCHEMA)
    barrier = Barrier(2)

    class ConcurrentStore(SelectionStore):
        @contextmanager
        def _tx(self) -> Iterator[sqlite3.Connection]:
            # Both stores reach their migration before either obtains its lock.
            barrier.wait(timeout=5)
            with super()._tx() as connection:
                yield connection

    def open_store() -> list[str]:
        store = ConcurrentStore(path)
        try:
            return [row["name"] for row in store._conn.execute("PRAGMA table_info(selection_decisions)")]
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: open_store(), range(2)))
    assert all(columns.count("candidate_id") == 1 for columns in results)
    SelectionStore(path).close()
