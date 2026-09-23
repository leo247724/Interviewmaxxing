# Local MVP handoff — September 22, 2026

The requested local backend/frontend MVP is integrated and tested. Verified code head is `53f3560c3857e79e21b4e84eaa2363440d7cb230`; subsequent final-receipt edits are documentation only. See [INTEGRATION_STATUS.md](../INTEGRATION_STATUS.md) for exact package checkpoints, acceptance results, limits and historical Superset IDs.

## Running and verified

Coordinator: `/Users/leo/.superset/worktrees/Interviewmaxxing/caramel-ketch`, branch `j-workspace`. Main dashboard is `http://127.0.0.1:4317/pipeline`, service `http://127.0.0.1:8765`. Both remain running; temporary test servers/browsers were closed. Service health reports all modules available, idle, TEST_ONLY. Restart instructions are in root README.

Root checks: locked Python install, Ruff, mypy (96 files), 1585 backend/e2e tests passed with 7 opt-in skips; frontend npm ci/typecheck/build, 87 unit tests, 111 browser tests with 1 known mobile keyboard skip; 5 actual frontend-to-service-to-runner-to-Chromium-to-localhost ATS flows passed; 29 offline performance prototype tests passed. All 3 fictional marketing applications had exactly 1 ATS acceptance. Site receipts, selected-job identity, resume hashes, missing-input reload, duplicate prevention, uncertain outcomes, task/cache persistence and pipeline import/edit/reimport were verified.

Independent final approvals: runtime/service 41 checks at `bd45264`; actual Chromium 5 probes; UI 17 checks at `dce1362`. Final package heads: core `f38d0d1`, browser `c3a3c41`, jobs `ab8f22d`, selection `d933069`, pipeline `e5c14c7`, service `069db47`, frontend `568d354`, writing `dbd8a09`, integration design `c64d15e`, performance `06ce7c9`. Parent union-only benchmark import sorting is `6cac84a`; numerical behavior unchanged.

Private final receipt: `.imx/mvp-verification.private.json`. Frontend evidence: `apps/web/output`. Worker source/provider receipts are ignored in `build/job-ingestion/.imx/source-smoke` and `build/jev-selection/artifacts/jev-final-acceptance-20260922` under the worktree parent.

## User preferences and private data

Performance-marketing operator fit is semantic: paid acquisition/media, performance/growth, demand generation, digital marketing and related lead/director responsibilities. Pure data/platform engineering is outside the focus. Strong Austin onsite/hybrid priority over US-wide remote; Texas-only remote is not equivalent. USD100000/year minimum. Main local preferences now explicitly hold unknown/noncomparable pay for review; library default KEEP remains configurable.

Default home `/Users/leo/.interviewmaxxing` has 8 imported tracker records/all 23 fields and the provisional resume-derived profile. Personal application count is verified 0 after all tests. The supplied Numbers/PDF files remain unchanged; imported source snapshots and immutable resume are retained. Final candidate JSON/contact/consent details remain pending for personal applications. No personal data was used in synthetic applications.

Numbers import receipt: `.imx/pipeline-import-receipt.private.json`; resume import: `.imx/resume-review/import-receipt.private.json`. TogetherWork assessments are completed in editable tracking, with original source preserved. Root `env.local` is ignored, server-only; never print its contents.

## Boundaries and future work

Live discovery succeeded in bounded Austin and separate remote smokes across all 4 sources (3 listings/source/leg, 1 page plus 1 detail). Gaps remained explicit; no source access wall in these samples. Final tiny fictional Jev smoke made 6 HTTP 200 requests, returned `typesafe/jev-1.13-20260917`, 0.57-0.63 seconds/selection, USD0.00053046 total. Austin acquisition APPLY, engineering SKIP, remote low-confidence REVIEW; Texas-only/estimated-pay cases held without calls. These are limited observations, not broad quality/capacity evidence.

TEST_ONLY checks initial application URL admission, not every redirect. Main runner uses Playwright; native accessible HTML forms are supported without dedicated employer adapters. OpenCLI host upload returns Not allowed; manual exact-file attachment plus digest verification is required. No real employer acceptance, personal application, bulk run, outreach, push or deployment occurred.

Email/Google Calendar/Cal remains a design; Gmail/Cal.com assumptions are unanswered. W1 is an offline source-bound writing prototype, without live prose or PDF/DOCX export. P0 is a reviewed offline queue/benchmark prototype, not production concurrency. 1000/day remains modeled demand of 50 browser-hours plus 29.4 human-hours. Measure large-backlog claim/heartbeat latency before queue integration; keep production two-stage Jev decisions until a held-out quality comparison supports a change.

## Ownership on future continuation

Eight implementation trees plus performance are retained under `/Users/leo/.superset/worktrees/Interviewmaxxing/build/`. Native Astra High workers completed the Claude-limit takeover; Astra Max frontend/review gates passed. No unfinished MVP worker task remains. Do not restart Claude merely because its reported 9:30pm reset arrived. Assign a new bounded package at a clean ownership boundary; one writer per file. Use native followup_task to wake completed agents, since send_message alone does not do so. Historical CLI IDs are in INTEGRATION_STATUS; verify the shell/session and no active writer before reuse.

Original `/Users/leo/.superset/projects/Interviewmaxxing` has unborn main and must not be repaired or overwritten as part of this work. No push occurred. The next user-directed work can supply the candidate JSON, enable a specifically chosen live application, or advance the documented integrations/writing/scale design.
