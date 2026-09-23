# Interviewmaxxing integration status

The local MVP is integrated and verified in `caramel-ketch`, branch `j-workspace`, on September 22, 2026. Verified code head: `53f3560c3857e79e21b4e84eaa2363440d7cb230`. All implementation and review gates for this milestone passed. The eight implementation worktrees and the additional performance worktree are retained for future work.

## Running locally

- Dashboard: [Pipeline](http://127.0.0.1:4317/pipeline), [Jobs](http://127.0.0.1:4317/jobs), [Desk](http://127.0.0.1:4317/).
- Service: `http://127.0.0.1:8765`; health reports all modules available, executor idle, `TEST_ONLY`.
- Main runtime home: `/Users/leo/.interviewmaxxing`, candidate `default`. The actual eight imported tracker records are visible. Personal application count is zero.
- Search preferences: semantic performance-marketing operator fit; strongly prefer Austin onsite/hybrid; US-wide remote included; USD100000/year floor. The local saved preference holds unknown/noncomparable pay for review. Library default `KEEP` remains configurable.
- Start/restart commands are in [README.md](README.md). Both the service and frontend bind loopback and use the exact frontend origin. The ignored `env.local` is loaded only by the service for Jev.

## Delivered behavior

OpenCLI discovery reads LinkedIn, Built In, Indeed and Google. Listing identity, source evidence, partial results and access problems remain explicit. Jev makes auditable semantic decisions through OpenRouter with candidate-scoped evidence/cache, confidence holds and deterministic constraint checks. Search and decisions do not submit applications.

The pipeline preserves all 23 reference fields, original imports, edits, lane history and optimistic revisions. Reimport is idempotent. A selected listing/card can hand off to the application desk, whose canonical application ID and receipt authority appear on the tracker.

The desk uses the Python runner and its single authoritative SQLite application store. It pins the selected resume and any proven selected-job identity before execution, checks fresh identity before actions, asks for missing facts or browser interaction, and records durable outcomes. Ambiguous submission stays locked for reconciliation. User reports remain distinct from site confirmation; an unreceived report does not permit a blind second submission. Startup ownership, aliases, repeated requests and restart recovery are covered.

## Final coordinator verification

| Check | Result |
| --- | --- |
| `uv sync --locked --all-packages` | PASS |
| `uv run --no-sync ruff check .` | PASS |
| `uv run --no-sync mypy` | PASS, 96 source files |
| `uv run --no-sync pytest tests e2e -q` | 1585 passed, 7 opt-in skipped, 131.34 seconds |
| Frontend `npm ci`, typecheck, production build | PASS |
| Frontend `npm test` | 87 passed |
| Frontend `npm run test:e2e` | 111 passed, 1 existing mobile keyboard skip, 43.6 seconds |
| `npx playwright test --config=playwright.live.config.ts` on final root | 5 passed, 15.0 seconds |
| Offline performance prototype unittest suite | 29 passed |

The final frontend-to-service-to-runner-to-Chromium tests used a newly created fictional home and a separate localhost marketing ATS. All three fictional applications had exactly one server-side acceptance. Checks covered matching expected job identity, receipt linkage/authority, exact uploaded bytes, a second resume, missing answers across reload, duplicate requests, uncertain submission and site reconciliation, Jev task/cache persistence, preferences, tracker edits and all-column import/reimport. Source adapters and Jev transport are fixtures in this suite; application execution is real.

The seven backend opt-ins are five OpenCLI host checks, one job-source smoke, and the private reference-workbook test. These capabilities were checked separately in their authorized live/private runs. The bounded source and provider checks below are distinct from the offline automated suite.

The ignored local receipt is `.imx/mvp-verification.private.json`; frontend traces/screenshots are under `apps/web/output`. Temporary acceptance services on 4382/4383 and 4392/4393, their ATS processes and task-owned inspection browsers were closed. Main services 4317/8765 remain running.

## Accepted worktree checkpoints

| Worktree | Final package/checkpoint | Review |
| --- | --- | --- |
| core-contracts | `f38d0d1` runner/store, expected identity and resume pinning | Core regressions; final runtime and Chromium gates approved |
| candidate-brain | Candidate `9eb9f72`; integration design `c64d15e` | Candidate review approved; X1 documentation reviewed |
| application-packets | Pipeline `e5c14c7`; writing prototype `dbd8a09` | Pipeline review approved; generation tests passed |
| browser-ats | `c3a3c41` including OpenCLI fix `0e4273a` | Independent confirmation and OpenCLI reviews approved |
| job-ingestion | `ab8f22d` | Astra Max approved; bounded Austin and remote source smokes passed |
| jev-selection | `d933069` | Astra Max approved; bounded final live Jev smoke passed |
| queue-runtime | `069db47`, reviewed code `bd45264` | 41 runtime checks and 5 real Chromium probes approved |
| dashboard | `568d354`, reviewed UI `dce1362` | 17 independent UI checks approved; final real acceptance passed |
| performance-runtime | `06ce7c9`, design/prototype approved at `63acbc7` | Astra Max approved; root import-order integration at `6cac84a` |

Claude reached its session limit during implementation. Per the user, Astra High completed implementation and Astra Max handled frontend and parallel reviews. Completed work does not need to restart at the reported 9:30pm Claude reset. Any future switch must happen at a task boundary with one owner per file. Native agent messages do not automatically restart completed agents; use a follow-up task when assigning new work.

## Bounded live evidence

J1 at `ab8f22d`: each of the four sources returned three listings in the Austin smoke and three in the separate US-remote smoke, within one search page and one detail attempt per source/leg. Source/job identity checks passed; PARTIAL reflected the cap. No access walls were observed in those samples. A Google result with Austin residency restrictions remained unresolved for nationwide eligibility. Some descriptions and eligibility details remained incomplete. Receipt: `build/job-ingestion/.imx/source-smoke/summary.md` relative to the worktree parent.

J2 at `d933069`: exactly six real HTTP requests, all 200, returned `typesafe/jev-1.13-20260917`. The fictional Austin Acquisition Director was APPLY; Data Platform Engineer was SKIP; a suitable US-remote role was held because the model's APPLY confidence was 0.77, below 0.80. Texas-only remote and estimated pay were local REVIEW holds with zero calls; the pay case explicitly used the review preference. Selection latency was 0.57-0.63 seconds; reported total cost USD0.00053046. Receipt: `build/jev-selection/artifacts/jev-final-acceptance-20260922/receipt.json`. This small sample does not establish population quality or calibration.

## Private inputs and remaining boundaries

The supplied Numbers workbook remains unchanged. Eight records and all 23 fields were imported, with zero duplicates on reimport. TogetherWork's completed assessments are reflected in editable tracking while the original source snapshot is preserved. The supplied resume remains an immutable private artifact with source-backed profile facts; the final candidate JSON is still pending. No personal application was created or submitted during testing.

The completed milestone is local and uses accessible native HTML forms. There are no dedicated production ATS adapters or verified employer-site acceptance results. TEST_ONLY admission checks the supplied URL; it is not full redirect-egress isolation. Actual OpenCLI upload is denied by the host and requires manual attachment of the exact file followed by digest verification; the main application runner uses Playwright.

Email, Google Calendar and Cal integration are [design only](docs/integrations/README.md), with Gmail and Cal.com provisional. [Document generation](docs/documents/README.md) is an offline, source-bound prototype; live prose generation and PDF/DOCX tailoring are future work. The [performance design/harness](docs/performance/benchmarks.md) is not a production scheduler. Thousands/day remains modeled demand, not demonstrated throughput; the default 1000/day scenario requires 50 browser-hours and 29.4 human-hours. No bulk run, outreach, deployment or remote push occurred.

## Historical Superset control mapping

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
