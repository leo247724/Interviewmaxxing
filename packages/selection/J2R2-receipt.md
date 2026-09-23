# J2R2 review correction receipt — 2026-09-22

Parent: `acdd2e30e8421c63b36eb6627f96d6d1b0ade526`. Owned scope remains selection package and selection tests. Candidate-scoped API signatures are unchanged; README examples retain required candidate_id and latest_many.

Corrections to the five reproduced review groups:

- Compound remote eligibility preserves negative/conditional clauses and refuses a broad MATCH when any component is narrower or unknown. `United States (except California)`, `United States, excluding TX`, `United States; Texas only`, and unknown qualifiers become ambiguous and effective REVIEW even with injected provider APPLY. Plain US, US only, and US and Canada remain MATCH.
- Consecutive unknown locality/province components remain one place, and explicit country stays attached. `Austin, ON, Canada` and `Austin, Manitoba, Canada` cannot inherit a bare Austin match. Definite foreign-country mismatch remains SKIP.
- Contextual incomplete locations (`Multiple locations in Texas`, `Various locations in United States`, `Location: United States`, `Flexible location`) become UNKNOWN/REVIEW, retaining known Dallas/California mismatches and Austin variants.
- Legacy SQLite column inspection now occurs inside the same BEGIN IMMEDIATE transaction as ALTER/backfill. A deterministic two-thread barrier regression verifies simultaneous first opens both succeed with one candidate_id column.
- Identity scrubbing normalizes whitespace before matching and treats underscore separators as boundaries, including multiword employer names. Fictional injected-provider assertions cover Avery_metric, doubled spaces, tabs, and underscored employer names while existing Averyville negative checks remain green.

Rubric bumped to `jev-selection-rubric/2026-09-22.7` to invalidate old policy/projection decisions. Known limits remain finite location aliases and known-identity/contact scrubbing rather than general arbitrary-prose anonymization. The provider is never trusted to override a deterministic ambiguity hold.

Verification in dedicated ignored `.venv-task`:

- `.venv-task/bin/python -m pytest tests/selection tests/core/test_discovery.py -q`: 395 passed (341 selection, 54 core discovery).
- `.venv-task/bin/ruff check packages/selection tests/selection`: passed.
- `.venv-task/bin/mypy --strict packages/selection/src`: passed, 12 source files.
- `git diff --check`: passed.

Zero paid/live provider calls, private profiles, real sites, or applications. All provider fixtures fictional with injected transport; existing requested/returned model and audit provenance remain unchanged.
