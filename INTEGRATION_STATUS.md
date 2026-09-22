# Interviewmaxxing integration status

## Current objective

Build the supplied-URL application flow: the user chooses a job and asks to apply; the system uses verified candidate information to fill and submit it, verifies acceptance, and saves a receipt. Job discovery and Jev selection are deferred. The MVP uses four of the eight prepared worktrees.

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
| core-contracts | `6a218556-206b-446f-ab32-7659bd383217` | MVP planned; unstarted | Working Claude login; minimal contracts, SQLite store and CLI |
| job-ingestion | `60876c0b-328b-4fc8-ba66-5ef7028922eb` | parked | Later job-discovery milestone |
| jev-selection | `82112dc8-f635-439f-a61d-c9c33f6636e3` | parked | Later automated job-selection milestone |
| candidate-brain | `4d16e305-b074-4bdd-9ff9-f46a553ccfbf` | MVP planned; unstarted | Approved core contracts; user profile and resume for live application |
| application-packets | `7333147f-5e19-441c-b25c-e08dc161282e` | MVP planned; unstarted | Approved core and candidate contracts |
| browser-ats | `acff6e42-6b2f-407e-b5c8-0978a8017ca4` | MVP planned; unstarted | Approved core contracts; first application URL for target ATS |
| queue-runtime | `04292d12-950d-40a8-b8ca-355593be29d9` | parked | Later hosted/distributed execution |
| dashboard | `ec7d3896-d6e4-407e-85eb-a5c452224509` | parked | Later web interface and analytics |

All workers are on the local host. Their directories are `/Users/leo/.superset/worktrees/Interviewmaxxing/build/<workspace-name>`. No worker terminal or Claude session ID has been assigned yet. The existing coordinator remains at `caramel-ketch` on `j-workspace`.

## Control-path evidence — 2026-09-22

- Superset CLI/host 1.30.1 is authenticated and reachable on the local host.
- Workspace discovery, terminal discovery, terminal reads and terminal sends succeeded.
- The preset model list rejects the pinned `claude-opus-5-5` ID. An explicit Claude command can run in a Superset terminal.
- After the user updated Claude Code to 2.1.280, an explicit `claude --model claude-opus-5-5 --effort high` launch displayed **Opus 5.5 with high effort**.
- The model request still returned **Login expired · Please run /login**; `claude auth status` reported `loggedIn: false`.
- No assistant acknowledgment or follow-up round trip has succeeded yet. Do not describe model communication as verified until both complete.
- The task-owned probe is separate from the coordinator and from user-created terminals. It has no implementation assignment.

## Next action

When Claude sign-in is working, verify a two-message Opus 5.5 handshake and assign the minimal core scaffold, supplied-URL contracts, SQLite application/event store, CLI skeleton and contract checks. Candidate, packet and browser tasks follow those contracts. A real application run also needs the user's target URL and verified profile/resume; no Jev setup is required for this milestone.
