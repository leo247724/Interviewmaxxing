# Interviewmaxxing worktrees

Eight implementation worktrees and one performance worktree are coordinated by Astra. The user expanded the MVP to include OpenCLI job discovery, Jev selection through OpenRouter, and a pipeline tracker. Every worker receives a bounded implementation or review package.

Current ownership is in [INTEGRATION_STATUS.md](INTEGRATION_STATUS.md). After Claude reached its September 22 usage limit, the user assigned Astra High implementation workers and Astra Max reviewers; the frontend uses Astra Max. Native workers communicate through the agent mailbox while editing their assigned worktrees. Historical Superset terminal sessions are idle and remain available for inspection. A return to Claude requires a clean task-boundary handoff so there is only one writer for each file.

## Workspace scope

All eight isolated implementation worktrees have been created in the Interviewmaxxing Superset project. They initially branched from `j-workspace` at `c68643a`. A separate `build/performance-runtime` worktree contains the reviewed design and offline benchmark prototype. Refresh the shared documentation from the coordinator before dispatching a task.

| Workspace name | Branch | MVP status | Ownership |
| --- | --- | --- | --- |
| `core-contracts` | `build/core-contracts` | In scope; first | `packages/core`, `apps/cli`, Python configuration, canonical contracts, SQLite application/event records and CLI integration |
| `job-ingestion` | `build/job-ingestion` | Active | `packages/jobs/**`: OpenCLI searches, observed job data, per-source state and deduplication |
| `jev-selection` | `build/jev-selection` | Active | `packages/selection/**`: Jev Decisions API through OpenRouter, preferences and auditable selection |
| `candidate-brain` | `build/candidate-brain` | In scope | `packages/candidate`, verified profile, supplied resume, saved screening answers and factual provenance |
| `application-packets` | `build/application-packets` | In scope | Reviewed `packages/generation`; now `packages/pipeline/**` for durable tracking and the reference-workbook importer |
| `browser-ats` | `build/browser-ats` | In scope | Browser/form inspection, document upload, filling, submission, confirmation and the first target ATS; covers WT-05 + WT-06 |
| `queue-runtime` | `build/queue-runtime` | In scope for local bridge only | `apps/service/**`, `tests/service/**`: loopback HTTP presentation service using the canonical Python executor/store. Hosted API, Redis and distributed execution remain deferred. |
| `dashboard` | `build/dashboard` | In scope; parallel | `apps/web/**`: application desk, profile/resume setup, progress/questions/receipts, jobs search and pipeline board |

Astra keeps the current coordinator worktree for architecture, task assignment, integration and verification. Shared files have one owner; each task gets an explicit file allowlist, including fixtures. Root dependency and lockfile changes go through the core owner. The MVP CLI lives in `apps/cli`; core defines its interfaces before parallel edits begin.

## Start order

1. **Core first:** establish minimal Python contracts, the local application/event store, submission states, a CLI skeleton, shared fixtures, verification commands, and `CONTRACTS.md`.
2. **Candidate and browser work:** build the verified profile/resume loader and live form execution against the approved contracts. The supplied URL determines the first supported ATS.
3. **Packets:** resolve form answers from candidate data and surface only genuinely missing required input. No discovery, Jev decision, ranking, or TypeSafe access is required.
4. **Core integration:** connect the packages into `interviewmaxxing apply <application-url>`. Verify that it submits the user's requested application, observes acceptance, and returns a saved receipt. Also verify missing-input resume, duplicate prevention and uncertain-submission recovery.

In parallel, the browser worker can build an independent localhost fixture server before contracts are ready. The dashboard worker builds the frontend against a narrow service interface, then connects it through the local service to the approved executor. The local service owns HTTP view models and background dispatch, not a second application state machine. The dashboard's Node manifest and lockfile stay under `apps/web`; it does not edit shared Python configuration.

The user's request to apply supplies job choice and submission authorization. Ask only for information or interactions required to complete that request. Keep the parked worktrees available without starting their roadmap tasks. Active workers use separate SQLite databases, browser profiles, artifacts and mock-server ports for their tests.

## Superset control protocol

This is the verified terminal control path for Claude sessions. During the Astra takeover, use the native owner's mailbox; do not start another writer in these terminals. A completed native agent needs a follow-up task to start a new turn; a message alone does not reactivate it.

Superset provides terminal transport. Astra tracks dependencies, task ownership and completion. The command families are documented in the [Superset CLI reference](https://docs.superset.sh/cli/cli-reference) and [orchestration guide](https://docs.superset.sh/orchestration).

Discovery:

```bash
superset workspaces list --local --project Interviewmaxxing --json
superset terminals list --local --workspace "$IMX_WORKSPACE_ID" --json
```

For each worker, retain its task ID, workspace ID, actual directory, branch, base commit, terminal ID, Claude session ID when available, verified model, status, dependencies and result. Terminal presence alone is not proof of progress or completion.

Launch an initial read-only handshake and verify that the actual Claude session uses Opus 5.5 before assigning implementation. On hosts whose model list includes it, use `superset agents create --agent claude --model claude-opus-5-5`. The current host's pinned-model list must be checked at launch; the `opus` alias needs verification of the resolved model.

An explicit Claude command can also be launched through `superset terminals create --command`, using Claude's own `--model claude-opus-5-5` flag. This keeps terminal control available when the Superset preset model list lags a release. Authentication and Claude's own model support must still be verified.

After launch, send task prompts and follow-ups to that worker's verified interactive Claude terminal:

```bash
superset terminals send --local \
  --workspace "$IMX_WORKSPACE_ID" --terminal "$IMX_TERMINAL_ID" \
  --text 'The bounded task or follow-up goes here.' --json

superset terminals read --local \
  --workspace "$IMX_WORKSPACE_ID" --terminal "$IMX_TERMINAL_ID" \
  --max-lines 240 --json

superset agents read --local \
  --workspace "$IMX_WORKSPACE_ID" --terminal "$IMX_TERMINAL_ID" --json
```

Read the terminal before sending. A shell prompt, trust screen, login screen or model menu requires different input from a ready Claude conversation. A send receipt proves only delivery to the terminal; require an actual assistant acknowledgment or result. Read all running workers at a measured cadence, with longer transcript reads when needed.

The verified control path also supports finite `claude --print` tasks with persistent provider sessions. Launch the exact model, retain the JSON `session_id`, and check `modelUsage`. When the process has finished and the terminal is observably back at its shell prompt, send a shell-quoted `claude --print <follow-up> --resume <session-id> --model claude-opus-5-5` command through the same terminal. `agents read` exposes its conversation while it works. Use task-scoped tool permissions; do not enable blanket permission bypass. The initial and resumed handshake on 2026-09-22 verified this path with actual assistant responses.

Each assignment includes its objective, approved contract/base commit, allowed files, dependencies, acceptance criteria, verification commands and completion format. Workers return the architecture's completion receipt and the `SUPERSET_WORKER_DONE` or `SUPERSET_WORKER_BLOCKED` marker. Astra checks the reported diff and tests before integration, records the exact result commit, and sends dependency updates to the affected workers.

Recover a lost terminal mapping with `terminals list`. Where the provider session ID is known, `agents create --resume-session` can restore its conversation. Verify the recipient and model after recovery. Do not launch a duplicate writer just because a screen is quiet. Completed implementation terminals remain available for inspection; only task-owned temporary probe terminals are disposable during setup.
