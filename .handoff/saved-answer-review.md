# Saved-answer review corrections

Coordinator review of C2 `d43ccd2` and C3 `61d89af`; all reproductions use fictional data. Each owner retains the allowlist and worker protocol in `mvp-build-tasks.md`. Commit the bounded correction, report exact checks and commit, and keep the Superset terminal available. Do not alter unrelated worktrees, root dependencies, external services, or real candidate data.

## C2R — Preserve conflicts and confirmation freshness

Owner: candidate-brain. Allowed writes: the C2 allowlist only.

1. `answers.py:97–99` excludes tied conflicts; `store.py:184–186` publishes only `result.kept`. With GLOBAL salary 100000 and two JOB answers at the same confirmation time for the same question/job, values 150000 and 175000, loading drops both JOB answers and the resolver silently answers 100000. Preserve tied conflicting SavedAnswer objects in CandidateProfile.saved_answers while continuing to report AnswerConflict; exclude superseded answers only. The resolver already surfaces applicable conflicting JOB answers as AMBIGUOUS.
2. `store.py:229–231` replaces same-question answers regardless of confirmed_at. Save old T0, then new T1, then retry old T0 currently leaves only old. Compare confirmation time under the existing lock, retain the newest, and preserve equal-time disagreements rather than choosing the last writer. Cover duplicate retries and explicit false/zero.

Add focused regression tests for load and actual persisted save behavior. No schema redesign. Run candidate tests, scoped lint and strict type checks. Explain the resulting kept/conflicts semantics in package documentation.

## C3R — Preserve question meaning

Owner: application-packets. Allowed writes: the C3 allowlist only.

`questions.py:54–64` removes <, >, $, and € with punctuation stripping. `is_neutral_hint` ignores nonalphabetic symbols. A saved Yes for “Have you managed a budget > $100,000?” incorrectly answers “< $100,000?”; saved salary “Expected salary $” fills a field with label “Expected salary” and placeholder “€”. Preserve meaning-bearing comparisons, currency and units, and do not classify symbolic units as neutral formatting hints. Keep harmless spacing/case normalization and documented genuinely neutral hints where safe. Add direct and resolver-level regressions.

`resolver.py:412–424` prompts omit placeholders. Display the complete question wording, including meaningful placeholders. Core is adding a shared raw full-question renderer and preserving full wording through existing MissingInput.label -> UserInput.question -> SavedAnswer.question fields in C1R3. Do not change core files; report your preferred renderer seam and use your existing QuestionText implementation until that core commit is provided. After refresh, add a resolver roundtrip test for help text plus placeholder using actual UserInput.answering(...).to_saved_answer(...), not a hand-built replacement question.

Run generation tests, scoped lint and strict type checks; commit the independent corrections, then await the core commit for the roundtrip follow-up if necessary.

## C1R3 — Preserve full wording when saving a user answer

Owner: core-contracts. Allowed writes: packages/core/**, tests/core/**, tests/fixtures/core/**, CONTRACTS.md only. Do not modify other packages or shared dependencies in this correction.

`packets.py:383–388` and `:407–412` use only the field label for UserInput.question; `to_saved_answer` copies it. For an ATTESTATION label “I agree” with help text “I certify that all application information is accurate.”, answer -> save -> resolve on the identical form fails to reuse because generation correctly matches the complete wording.

Provide one raw full-question renderer for label + help_text + placeholder in a stable documented order. MissingInput.for_field must carry that complete wording in its existing user-facing label, and UserInput.for_field must use it for question. UserInput.answering and to_saved_answer then preserve it. Keep SavedAnswer schema and core public contracts compatible; no broader matching or relaxed provenance. Ensure readable formatting and omit empty components. Add regression tests covering label, help and placeholder, including a meaningful currency placeholder and same-label changed help. Update CONTRACTS.md with the renderer seam and integration instructions. Report exact source/API so C3R can consume it.

Run focused and full core tests, scoped ruff and strict types. The integrated baseline includes 16 existing ruff findings in browser fixture files; report them without modifying browser-owned files. Browser owner is responsible for those.
