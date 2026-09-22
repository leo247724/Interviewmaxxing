# Interviewmaxxing integration status

## Current objective

Build the supplied-URL application flow: the user chooses a job and asks to apply; the system uses verified candidate information to fill and submit it, verifies acceptance, and saves a receipt. The user explicitly resumed the workers and requested the entire backend and frontend on 2026-09-22. Job discovery and Jev selection are deferred. The MVP now uses six of the eight prepared worktrees, including a local HTTP service in queue-runtime.

## Repository baseline

- Coordinator branch: `j-workspace`.
- Upstream baseline: `9afc591` (architecture only).
- Shared setup documents: `ARCHITECTURE.md` and `WORKTREES.md`.
- All eight feature-worker worktrees have been created from the shared documentation checkpoint `c68643a4286d39c69ec2709684de72bbcea490b4`.
- At creation, each worktree was verified clean, on its intended `build/<workspace-name>` branch, with HEAD at that checkpoint. The coordinator owns subsequent scope checkpoints; verify that each worker has the current documentation before dispatch.
- Reviewed core contracts through `462476ccb2b4fac8913b58af4e8fd011b549b508` are integrated. `CONTRACTS.md` publishes version 2 interfaces and `sh scripts/verify.sh` is the executable verification command.

## Worker board

| Worker | Superset workspace ID | Status | Next dependency |
| --- | --- | --- | --- |
| core-contracts | `6a218556-206b-446f-ab32-7659bd383217` | C1R3 `4fd2959` done, in review | C2/C3/C4 integration before I1 |
| job-ingestion | `60876c0b-328b-4fc8-ba66-5ef7028922eb` | parked | Later job-discovery milestone |
| jev-selection | `82112dc8-f635-439f-a61d-c9c33f6636e3` | parked | Later automated job-selection milestone |
| candidate-brain | `4d16e305-b074-4bdd-9ff9-f46a553ccfbf` | C2/C2R through `fa99724` done, in review | C2P profile editing and resume uploads next |
| application-packets | `7333147f-5e19-441c-b25c-e08dc161282e` | C3 `61d89af` done; C3R running | Preserve meaningful symbols, then C1R3 roundtrip |
| browser-ats | `acff6e42-6b2f-407e-b5c8-0978a8017ca4` | C4 `bb1bb3a` done, in review | Independent runtime review and integration; OpenCLI driver follow-up |
| queue-runtime | `04292d12-950d-40a8-b8ca-355593be29d9` | S1 local HTTP service running | I1 reusable runner and C2P setup API; hosted/distributed work deferred |
| dashboard | `ec7d3896-d6e4-407e-85eb-a5c452224509` | F1R `620fb97` done; focused tests/types pass; visual recheck running | S1/I1 for real F2 integration |

All workers are on the local host. Their directories are `/Users/leo/.superset/worktrees/Interviewmaxxing/build/<workspace-name>`. The existing coordinator remains at `caramel-ketch` on `j-workspace`.

Bounded assignments and acceptance requirements are in [.handoff/mvp-build-tasks.md](.handoff/mvp-build-tasks.md). C1 starts from `9111218`; later tasks receive its reviewed result before dispatch.

| Task | Terminal | Claude session | Verified model | Result |
| --- | --- | --- | --- | --- |
| control handshake | `4fcc3a93-dbde-40f5-9ac8-47415b2c8c83` | `a953ca77-51e6-4b01-89cd-d7527d957d1a` | `claude-opus-5-5` from `modelUsage` | Initial and resumed assistant acknowledgments verified through terminal reads and `agents read` |
| C1 / C1R / C1R2 | `4fcc3a93-dbde-40f5-9ac8-47415b2c8c83` | `c7b3b809-7801-421c-95c1-499f7330cd14` | `claude-opus-5-5`, verified from task assistant messages | Integrated through `462476c`; coordinator independently passed all 218 tests, ruff, strict mypy, locked sync and CLI smoke |
| C4a | `599558c3-fd93-43ce-a29c-9c9702b23b69` | `5947e2df-8de9-43a3-ae01-09ac9898b1af` | `claude-opus-5-5`, verified from task assistant messages | `2382f65` independently verified and merged as `f2e3d6b`; terminal kept for C4 |
| C2 / C2R | `8ea7cdf0-d32c-4938-87fa-df5f94f48ec1` | `612aa043-1c46-4250-83a7-f6e9924410ea` | `claude-opus-5-5`, verified from task assistant messages | `fa99724`; loader conflict and stale-save corrections under independent review |
| C3 / C3R | `277d2333-35ea-4ebb-83d4-3f0985a2eaa5` | `052fc903-c0a3-451e-8d73-48aa674890e7` | `claude-opus-5-5`, verified from task assistant messages | `61d89af`; question matching correction running |
| F1 / F1R | `043d3f36-8a95-49a6-bc97-ea1255a7283f` | `7741a68f-82d8-4d3a-89fd-41dab783e3f6` | `claude-opus-5-5`, verified from task assistant messages | `620fb97`; coordinator passed all 6 recovery tests and typecheck; visual review running |
| S1 | `6aeb2dec-1fa7-4e68-9ca9-d88da8f2c7d6` | `c0180abb-71bb-400f-9d79-2b833e52014d` | `claude-opus-5-5`, verified handshake `modelUsage` | Local service implementation dispatched; allowlist `apps/service/**`, `tests/service/**` |

## Completed verification

- C4a changes are confined to its six allowed fixture files. The coordinator reran all 28 stdlib HTTP tests successfully on Python 3.12 and checked the text diff. PDF cross-reference trailing spaces are valid fixture bytes and were excluded from text whitespace checking.
- The coordinator started an isolated mock server from the merged tree and used Playwright CLI/Chromium to fill native controls, select machine-backed choices and upload the fictional resume. The page showed acceptance for BWA-ENG-101 with reference `BWA-000001` at `2026-09-22T22:02:16Z`; an independent server query found exactly one accepted POST and the correct uploaded SHA-256.
- That coordinator-only browser session and mock server were closed cleanly. Snapshot evidence is under `/var/folders/wy/jv0dwczn75d5s_7w71jb7vpr0000gn/T/imx-coordinator-browser-4u0ulxsr/.playwright-cli/`; no real employer or candidate data was used.
- C1 review corrections are complete, including explicit saved-answer scope, verified facts and complete question wording in fingerprints. A changed attestation description invalidates the prior input and packet. The coordinator reviewed the final diff and reran all 218 tests plus formatting, strict types, locked sync and CLI smoke successfully.
- F1 independent UI review passed desktop and mobile compose, validation, missing answers, unselected attestations, draft save, receipt and uncertain reconciliation. F1R now preserves the active ID across transient service failures and clears it only for a genuine missing record. Coordinator recovery tests and types pass; visual follow-up is running.
- C2/C3 review found four reproducible defects: meaningful symbols lost by question matching, tied JOB conflicts removed before resolution, stale saves overwriting newer answers, and full wording lost in saved-answer roundtrips. Corrections are assigned in `.handoff/saved-answer-review.md`; completion requires independent verification and integration.
- C4 worker reports 332 passing tests, strict types/lint and a real browser demo with exactly one accepted POST. Independent review and coordinator browser tests are underway; these are not yet integrated acceptance results.
- OpenCLI 1.8.6 and Browser Bridge were verified live. In a named session, the assistant read current questions while the user controlled all assessment actions. All three assessment sections completed and the final page confirmed completion. This is evidence for user-present observation, not an automated application submission or a completed reusable site adapter. Private invitation data and assessment content remain outside source control.

## Control-path evidence — 2026-09-22

- Superset CLI/host 1.30.1 is authenticated and reachable on the local host.
- Workspace discovery, terminal discovery, terminal reads and terminal sends succeeded.
- The preset model list rejects the pinned `claude-opus-5-5` ID. An explicit Claude command can run in a Superset terminal.
- After the user updated Claude Code to 2.1.280, an explicit `claude --model claude-opus-5-5 --effort high` launch displayed **Opus 5.5 with high effort**.
- The earlier model request returned **Login expired · Please run /login**. A subsequent `claude auth login` completed successfully; a fresh auth check now reports `loggedIn: true`.
- The core worktree terminal returned actual assistant acknowledgments `IMX_CONTROL_ACK_20260922_A` and `IMX_CONTROL_ACK_20260922_B` across an initial and resumed request. Both responses reported `claude-opus-5-5` in `modelUsage`, with no permission denials. `agents read` independently exposed both assistant messages.
- Superset terminal transport also works with finite `claude --print` tasks. After a task exits to a verified shell, send a new command using `--resume <Claude session ID>` for follow-up. Do not send natural-language prompts to a shell or shell commands into a running model conversation.
- Login terminal `f9a78907-ec8c-4f77-86b5-26f93ffe11f3` is task-owned, in the coordinator workspace, and has completed sign-in. The earlier probe terminal has ended. User terminals remain untouched.

## Next action

Integrate the reviewed corrections and browser/frontend foundations, then run I1, C2P, S1 and F2 to finish the local application product. Exact scopes and seams are in `.handoff/local-service-integration.md`. The OpenCLI driver remains a separate browser follow-up sharing the same runtime. A real target application URL and verified candidate profile/resume are still needed for live employer acceptance; build and verify all independent functionality against fictional localhost fixtures first.
