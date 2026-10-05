# Worktree consolidation

The current `caramel-ketch` source is the publication source for remote `main`.

All existing worker branch code was already integrated except the final `build/wp1-round15` Wellfound changes, which are merged with the current implementation. The `build/classifier` documentation patch is already equivalent to an integrated commit. The detached dashboard acceptance worktree contains merge-only history without additional code.

The `archive` directory preserves three previously ignored historical smoke-script sources verbatim as `.py.txt`, plus the untracked performance review handoff. They are evidence from past runs, not current operational entrypoints; they contain historical paths and browser identifiers.

Local run receipts, screenshots, database artifacts, the original dirty source, and the previous remote-main commit were backed up under `/Users/leo/.superset/backups/Interviewmaxxing/20261005-033259`. Credentials and runtime state remain local. No other worktree was deleted.

## Verification

- Python lint and strict type checking pass.
- All 22 Wellfound browser regressions pass, including a pending submit request, refused dispatch, and non-native SPA submit controls.
- 160 integration regressions and 203 policy/company/Greenhouse checks pass.
- Web: 203 unit tests, TypeScript checking, isolated production build, and 34 desktop/mobile interview and pipeline browser checks pass.
- The broad Python suite was stopped after 1,571 passing tests; it was not completed.
- A separate run reproduced 49 pre-existing failures in tests of ignored personal-Chrome drivers. Their AST extraction and fake dependencies have drifted from the local operational drivers. These drivers are intentionally outside tracked source and the tests skip when they are absent. Active operational drivers were not modified.
