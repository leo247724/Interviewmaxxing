# J2R verification receipt — 2026-09-22

Parent checkpoint: `0f58421` (J2S). This checkpoint preserves and completes the inherited J2R edits. Owned scope: `packages/selection/**`, `tests/selection/**` only.

## Changes

- Candidate ID is included in the decision cache key and checked by `is_current`. All store retrievals are candidate-scoped, including get/audit; legacy rows migrate their candidate owner from `record_json`. Latest reads load one row, and `latest_many` returns only the latest candidate-owned result per requested listing, with indexed SQL chunked at 400 IDs.
- Location parsing distinguishes locality, region and country. Austin TX targets work without commas; Austin MN is different. Texas/United States-only onsite locations remain unknown and reviewable. Explicit Canada remote is outside the region and cannot yield APPLY; Texas-only and ambiguous eligibility cannot be treated as US-wide.
- Decision commands and AI-reviewer directives trigger a review hold without changing constant rubric questions. Ordinary quoted Apply-button instructions do not trigger that heuristic.
- Profile projection redacts known first/last/preferred/full names and employer/institution names from fact keys and values, experience and education text. It preserves qualifications and removes contact-bearing values. Employer names known from profile facts are also scrubbed from free-text evidence. Provider payload tests verify that neither candidate identity nor local candidate IDs are sent.
- Rubric version is `jev-selection-rubric/2026-09-22.6`; full-precision threshold strings prevent nearby custom thresholds sharing stale decisions.

## API

```python
SelectionService.select(listing, preferences, candidate, *, candidate_id=None, use_cache=True)
SelectionService.cache_key(listing, preferences, candidate, *, candidate_id=None)
SelectionService.is_current(selection, listing, preferences, candidate, *, candidate_id=None)
SelectionStore.latest(listing_id, *, candidate_id: str) -> SelectionOutcome | None
SelectionStore.latest_many(listing_ids: Iterable[str], *, candidate_id: str) -> dict[str, SelectionOutcome]
SelectionStore.history(listing_id, *, candidate_id: str) -> list[SelectionOutcome]
SelectionStore.get(selection_id, *, candidate_id: str) -> SelectionOutcome | None
SelectionStore.audit(selection_id, *, candidate_id: str) -> DecisionAudit | None
SelectionStore.find_cached(listing_id, candidate_id, cache_key) -> SelectionOutcome | None
```

Evidence supplies the owner; explicit method ID must agree. Without evidence, explicit ID overrides constructor default; missing both raises ValueError. Wrong-candidate reads return None/empty. Batch results omit missing IDs, deduplicate input, and break tied timestamps by latest insertion. Existing service adapters may use the candidate_id keyword on latest/get and prefer latest_many for listing pages.

## Verification

In the dedicated ignored `.venv-task`, editable local core and selection:

- `python -m pytest tests/selection tests/core/test_discovery.py -q`: **365 passed** (311 selection, 54 core discovery).
- `ruff check packages/selection tests/selection`: passed.
- `mypy --strict packages/selection/src`: passed, 12 source files.
- `git diff --check`: passed.

Regression files: `test_candidate_scoping.py` (cross-candidate cache/get/history/currentness, no-profile IDs, old-schema migration, batch ties/chunks, precise thresholds); `test_places.py`, `test_policy.py`, `test_location_priority.py` (place parsing, hard constraints and holds); `test_privacy.py` (identifier scrubbing and provider payload); `test_service.py` (constant questions and effective REVIEW under injected APPLY responses). Existing provider timeout/retry/errors/malformed probabilities and provenance tests also passed.

## Evidence and limits

All provider tests use fictional injected transports; this checkpoint made **zero paid/live provider calls**, fetched no real sites and opened no private profiles. Requested model remains `typesafe/jev-1.13`; fictional tests assert returned `typesafe/jev-1.13-20260917`, generation IDs, usage and raw response audit. Historical live observations already in README are not new verification by this checkpoint.

Location parsing is conservative, with finite country/state aliases; unfamiliar eligibility stays ambiguous. Missing remote eligibility may still be assessed by Jev from the observed description. Injection detection is supplementary to constant instructions and untrusted state, not exhaustive detection of arbitrary attacks. Privacy filtering redacts known identifiers and recognized contact patterns, not arbitrary unknown entities; direct CandidateEvidence construction requires already-minimized qualifications. No automatic application or submission behavior was added.
