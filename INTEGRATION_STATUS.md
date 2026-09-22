# Interviewmaxxing integration status

## Current objective

Prepare eight Opus 5.5 implementation worktrees and a verified Superset communication path. The first product milestone remains one job URL selected by Jev, followed by a truthful, inspectable application and a persisted outcome.

## Repository baseline

- Coordinator branch: `j-workspace`.
- Upstream baseline: `9afc591` (architecture only).
- Shared setup documents: `ARCHITECTURE.md` and `WORKTREES.md`.
- No application implementation or feature-worker worktrees have been created in this setup task.
- Core is the first implementation assignment. `CONTRACTS.md` and executable verification commands are outputs of that task, not existing implementations.

## Worker board

| Worker | Status | Next dependency |
| --- | --- | --- |
| core-contracts | pending creation | Common documentation base and working Claude login |
| job-ingestion | pending creation | Approved core contracts |
| jev-selection | pending creation | Approved core contracts; TypeSafe access for live integration verification |
| candidate-brain | pending creation | Approved core contracts |
| application-packets | pending creation | Approved core, candidate and selection contracts |
| browser-ats | pending creation | Approved core contracts; generic browser acceptance before adapters |
| queue-runtime | pending creation | Approved core contracts; module implementations for end-to-end verification |
| dashboard | pending creation | Approved core/event/API contracts; runtime API for live integration |

## Control-path evidence — 2026-09-22

- Superset CLI/host 1.30.1 is authenticated and reachable on the local host.
- Workspace discovery, terminal discovery, terminal reads and terminal sends succeeded.
- The preset model list rejects the pinned `claude-opus-5-5` ID. An explicit Claude command can run in a Superset terminal.
- After the user updated Claude Code to 2.1.280, an explicit `claude --model claude-opus-5-5 --effort high` launch displayed **Opus 5.5 with high effort**.
- The model request still returned **Login expired · Please run /login**; `claude auth status` reported `loggedIn: false`.
- No assistant acknowledgment or follow-up round trip has succeeded yet. Do not describe model communication as verified until both complete.
- The task-owned probe is separate from the coordinator and from user-created terminals. It has no implementation assignment.

## Next action

After Claude sign-in completes, verify the initial acknowledgment and a second message in the same session. Record the actual model and recipient mapping, then start the core task when the user has created its worktree and assigned the build.
