# Interviewmaxxing integration status

Build the complete local backend and frontend: supplied-URL applications, OpenCLI job discovery, Jev selection through funded OpenRouter, and the pipeline tracker. Continue through integrated testing. Personal applications remain off while final candidate JSON is pending. The user additionally permits synthetic application tests, with none of their personal information; hosted tests should use designated test/demo flows. No production deployment, remote push, outreach or bulk application run is part of this build.

## Confirmed product criteria

- Performance marketing operator fit by actual responsibilities, including paid acquisition, paid media, performance/growth marketing, demand generation and digital marketing leadership. Titles are representative search seeds, never an exact-title allowlist. Similar acquisition/lead/director roles qualify semantically.
- Seed titles: Paid Media Manager, Senior Paid Media Manager, Performance Marketing Manager, Growth Marketing Manager, Demand Generation Manager, Digital Marketing Manager, plus the original marketing manager/director. Pure data-platform engineering is outside the focus.
- Strongly prefer Austin onsite/hybrid over eligible nationwide US remote. Remote remains eligible and is never restricted to Texas. Minimum annual compensation USD100000; missing/noncomparable pay stays unknown.
- The tracker retains all23 fields and8 private records from the supplied Numbers workbook, with original source provenance and separately editable board lanes. Imports never manufacture application receipts.
- User supplied the résumé. Its private profile was imported with35 literal source-backed USER_STATED claims,6 experience records,1 education and the original PDF bytes. It is provisional for live applications until the user's final JSON arrives; no screening/consent answers were invented.
- The8 private tracker records and all23 fields have been imported and roundtrip checked. Reimport previews8unchanged/0duplicates. TogetherWork's completed assessments were recorded in editable tracking, preserving the original workbook/source snapshot.
- Email/Google Calendar/Cal tracking and document tailoring/cover letters are next-stage design/prototypes in separate bounded workers. Gmail and Cal.com are provisional pending clarification; no accounts are connected by this work.

## Coordinator and integrated work

Coordinator directory `/Users/leo/.superset/worktrees/Interviewmaxxing/caramel-ketch`, branch `j-workspace`; original upstream baseline9afc591. Latest semantic contract checkpoint `caae823`. Private runtime home `/Users/leo/.interviewmaxxing`, candidate id `default`; private profile/resume stay outside Git. Root `env.local` is ignored and never printed or sent to the frontend.

Reviewed and integrated: C1R3 core contracts/store; C2R candidate loading; C2P/C2P1/C2P2 through9eb9f72; C3R factual generation through9d6a142; F1R2 frontend through8f0da8a; D0 identity fixes and location preference throughac4d626 plus timestamp precision5e994ed. Root6e02c71 excludes Node apps/web from Python workspace and locks reviewed candidate/generation packages.

Coordinator checks: combined core/candidate/generation487 tests passed before latest candidate/semantic additions; latest54 discovery tests and144 candidate tests pass, scopedruff and strictcoremypy pass. D0 identity/enrichment/location/date fixes received independent approval. C2P2 independent144-test review and targeted corruption/concurrency probes approved. Supplied PDF digest matches stored copy; candidate reload and complete setup verified. Whole-product verification remains pending below.

## Worker board

All worktrees are under `/Users/leo/.superset/worktrees/Interviewmaxxing/build/<name>`. Claude hit its5-hour session limit on Sep22, resetting9:30pm America/Chicago. All Claude commands exited; the parent verified no active Claude process in these trees. Per the user, native Astra High workers now own implementation, Astra Max owns frontend, and Astra Max reviewers run in parallel. Old Superset terminals may show idle; do not start a second writer there. After9:30pm, any return to Opus requires a clean ownership handoff at a task boundary.

| Worker | Current package/checkpoint | Status and dependency |
| --- | --- | --- |
| core-contracts | I1R1408302 + B3fix72a10ea | `/root/core_astra_high`;331core+14CLIChromium tests; B3 independently approved. Final browser integration remains |
| candidate-brain | C2P2 9eb9f72 approved/merged; X1design | `/root/integrations_astra_high`; docs/integrations only, official API research and source-attributed sync design |
| application-packets | P1R2 e5c14c7 approved/merged; W1prototype | `/root/writing_astra_high`; factual document bundles/cover letters, generation scope only, no live model or automatic pin |
| browser-ats | C4R3 dee76b7 held; O1R/C4R4 corrections | `/root/browser_astra_high`; unique owned sessions, actual resume digest, abort on document loss, ambiguous record isolation |
| job-ingestion | J1R0a02462 | `/root/jobs_astra_high`;204tests+1live skip, lint/types pass; Astra Max review in progress |
| jev-selection | J2Racdd2e3 | `/root/selection_astra_high`;365tests/lint/types pass, candidate-scoped batch API; Astra Max review in progress |
| queue-runtime | S3R24f80d7, adapters5115dae, new acceptancecb5cf3f | `/root/service_astra_high`; final dependency integration, actual HTTP/CLI/Chromium/ATS acceptance; runtime Max review |
| dashboard | Astra UI4955b23 plus review corrections | `/root/frontend_astra_max`;86unit/101browser+1skip atcheckpoint, production screenshots; final actual-service flow and3scoped recovery fixes |
| performance-runtime | Fable design457d8ca + unfinished harness | `/root/performance_astra_high`; finish fictional benchmarks and incorporate `/root/performance_astra_max` review; no real throughput claim |

Live read-only reviews: `/root/review_runtime_max`, `/root/review_discovery_max`, `/root/verify_frontend`, `/root/review_core_contracts`, `/root/review_browser`. Review feedback goes directly to each sole writer; acceptance is tied to exact commits.

## Control mapping

| Worker | Workspace ID | Terminal ID | Opus session |
| --- | --- | --- | --- |
| core-contracts |6a218556-206b-446f-ab32-7659bd383217|4fcc3a93-dbde-40f5-9ac8-47415b2c8c83|c7b3b809-7801-421c-95c1-499f7330cd14|
| candidate-brain |4d16e305-b074-4bdd-9ff9-f46a553ccfbf|8ea7cdf0-d32c-4938-87fa-df5f94f48ec1|612aa043-1c46-4250-83a7-f6e9924410ea|
| application-packets |7333147f-5e19-441c-b25c-e08dc161282e|277d2333-35ea-4ebb-83d4-3f0985a2eaa5|052fc903-c0a3-451e-8d73-48aa674890e7|
| browser-ats |acff6e42-6b2f-407e-b5c8-0978a8017ca4|599558c3-fd93-43ce-a29c-9c9702b23b69|5947e2df-8de9-43a3-ae01-09ac9898b1af|
| dashboard |ec7d3896-d6e4-407e-85eb-a5c452224509|043d3f36-8a95-49a6-bc97-ea1255a7283f|7741a68f-82d8-4d3a-89fd-41dab783e3f6|
| queue-runtime |04292d12-950d-40a8-b8ca-355593be29d9|6aeb2dec-1fa7-4e68-9ca9-d88da8f2c7d6|c0180abb-71bb-400f-9d79-2b833e52014d|
| job-ingestion |60876c0b-328b-4fc8-ba66-5ef7028922eb|50f61059-be65-4169-98cf-f6c7e3742cfc|42491b53-c805-47f7-a057-2673ebd934f8|
| jev-selection |82112dc8-f635-439f-a61d-c9c33f6636e3|24ff5b4d-723a-4719-a46f-1654df2bdabf|8d49cec1-0ca1-4d5f-99d6-3aa21e74e66c|

The table retains historical CLI session mappings for later inspection. Fable I1R session is f563693b-534d-4fd7-a0ab-e14f7284b14a; limited W1/X1 sessions are fcf8b42e-8ac3-458c-a62a-9942be9eb55d and a0f5426e-da19-48f6-a1b9-a994d7afb5ec. Performance workspace907410ce-6121-4926-9782-c7a543cee411, terminal1e690689-5186-4901-855f-d49ad5546f12, prior Fable session3ea3d203-b4e1-4907-934f-766045c72ea7. All are currently inactive. Keep terminals/worktrees available, and verify a shell prompt plus sole ownership before a later CLI handoff.

## Remaining completion gates

Resolve browser and frontend findings, accept J1/J2 reviews, merge final packages, update the union Python lock and run integrated checks. Prove actual frontend->service->runner->Chromium->localhost ATS, exact uploaded bytes/onePOST, missing-input restart, duplicate prevention, uncertainty/reconciliation and resumeA/B pinning. Verify OpenCLI in an owned localhost session if supported; its file-upload host restriction must remain explicit. Test job/preferences/selection/pipeline routes, semantic fit and Austin/US-remote ranking, source login/partial states and responsive UI. The private8-row import is already complete. No real employer acceptance or thousands/day capacity is claimed.

Detailed current correction/seam brief: [.handoff/current-integration.md](.handoff/current-integration.md). Original scoped tasks: [.handoff/mvp-build-tasks.md](.handoff/mvp-build-tasks.md), [.handoff/local-service-integration.md](.handoff/local-service-integration.md), [.handoff/job-browser-selection.md](.handoff/job-browser-selection.md), [.handoff/pipeline-and-frontend.md](.handoff/pipeline-and-frontend.md).
