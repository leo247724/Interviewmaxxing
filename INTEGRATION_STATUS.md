# Interviewmaxxing integration status

## Current objective

Build the supplied-URL application flow: the user chooses a job and asks to apply; the system uses verified candidate information to fill and submit it, verifies acceptance, and saves a receipt. The user has also activated the frontend worktree to build the interface in parallel. Job discovery and Jev selection are deferred. The MVP uses five of the eight prepared worktrees.

## Repository baseline

- Coordinator branch: `j-workspace`.
- Upstream baseline: `9afc591` (architecture only).
- Shared setup documents: `ARCHITECTURE.md` and `WORKTREES.md`.
- All eight feature-worker worktrees have been created from the shared documentation checkpoint `c68643a4286d39c69ec2709684de72bbcea490b4`.
- At creation, each worktree was verified clean, on its intended `build/<workspace-name>` branch, with HEAD at that checkpoint. The coordinator owns subsequent scope checkpoints; verify that each worker has the current documentation before dispatch.
- Core is the first implementation assignment. `CONTRACTS.md` and executable verification commands are outputs of that task, not existing implementations.

## Worker board

| Worker | Superset workspace ID | Status | Next dependency |
| --- | --- | --- | --- |
| core-contracts | `6a218556-206b-446f-ab32-7659bd383217` | C1 running; package files being written | Minimal contracts, SQLite store and CLI; coordinator verification before downstream dispatch |
| job-ingestion | `60876c0b-328b-4fc8-ba66-5ef7028922eb` | parked | Later job-discovery milestone |
| jev-selection | `82112dc8-f635-439f-a61d-c9c33f6636e3` | parked | Later automated job-selection milestone |
| candidate-brain | `4d16e305-b074-4bdd-9ff9-f46a553ccfbf` | Opus handshake verified; C2 pending | Approved core contracts; user profile and resume for live application |
| application-packets | `7333147f-5e19-441c-b25c-e08dc161282e` | Opus handshake verified; C3 pending | Approved core and candidate contracts |
| browser-ats | `acff6e42-6b2f-407e-b5c8-0978a8017ca4` | C4a running: independent fixture server | C1 approval before C4 runtime; first application URL for target ATS |
| queue-runtime | `04292d12-950d-40a8-b8ca-355593be29d9` | parked | Later hosted/distributed execution |
| dashboard | `ec7d3896-d6e4-407e-85eb-a5c452224509` | F1 running; Next.js setup underway | Service interface first; integrated executor needed for F2 |

All workers are on the local host. Their directories are `/Users/leo/.superset/worktrees/Interviewmaxxing/build/<workspace-name>`. The existing coordinator remains at `caramel-ketch` on `j-workspace`.

Bounded assignments and acceptance requirements are in [.handoff/mvp-build-tasks.md](.handoff/mvp-build-tasks.md). C1 starts from `9111218`; later tasks receive its reviewed result before dispatch.

| Task | Terminal | Claude session | Verified model | Result |
| --- | --- | --- | --- | --- |
| control handshake | `4fcc3a93-dbde-40f5-9ac8-47415b2c8c83` | `a953ca77-51e6-4b01-89cd-d7527d957d1a` | `claude-opus-5-5` from `modelUsage` | Initial and resumed assistant acknowledgments verified through terminal reads and `agents read` |
| C1 | `4fcc3a93-dbde-40f5-9ac8-47415b2c8c83` | `c7b3b809-7801-421c-95c1-499f7330cd14` | `claude-opus-5-5`, verified from task assistant messages | Writing contracts and store; no completion receipt yet |
| C4a | `599558c3-fd93-43ce-a29c-9c9702b23b69` | `5947e2df-8de9-43a3-ae01-09ac9898b1af` | `claude-opus-5-5`, verified from task assistant messages | Running; no completion receipt yet |
| Candidate readiness | `8ea7cdf0-d32c-4938-87fa-df5f94f48ec1` | `a02f1151-7b8a-4f99-bdbb-3cf48fa103b8` | Actual acknowledgment and `claude-opus-5-5` model usage verified | No implementation assigned yet |
| Packet readiness | `277d2333-35ea-4ebb-83d4-3f0985a2eaa5` | `aa8b5af2-edcb-4a1e-81db-4788ff33874c` | Actual acknowledgment and `claude-opus-5-5` model usage verified | No implementation assigned yet |
| F1 | `043d3f36-8a95-49a6-bc97-ea1255a7283f` | `7741a68f-82d8-4d3a-89fd-41dab783e3f6` | `claude-opus-5-5`, verified from task assistant messages | Next.js dependencies installed; frontend implementation in progress |

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

Monitor C1, C4a and F1 through Superset, inspect actual code and test results, and integrate approved contracts. Then dispatch candidate and browser runtime work, followed by packet resolution, CLI integration and the real frontend bridge. A read-only reviewer is inspecting the emerging core contracts while the coordinator manages the other workers. The user has been asked for the target URL and verified profile/resume source paths needed for real acceptance; local fixture development proceeds independently. No Jev setup is required for this milestone.
