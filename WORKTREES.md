# Interviewmaxxing worktrees

The build uses eight Claude Opus 5.5 workers plus the existing Astra coordinator. Architecture IDs describe responsibilities; they do not require nine separate worker sessions. One worker owns both WT-05 browser runtime and WT-06 ATS adapters.

## Workspaces to create

Create isolated local worktrees in the Interviewmaxxing Superset project. Use the coordinator's `j-workspace` branch as the initial base after the shared documentation checkpoint is committed. Workers remain unassigned until Astra sends a bounded task.

| Workspace name | Branch | Architecture scope | Ownership |
| --- | --- | --- | --- |
| `core-contracts` | `build/core-contracts` | WT-00 | `packages/core`, migrations, root package/test configuration, shared schemas and fixtures, contract definitions |
| `job-ingestion` | `build/job-ingestion` | WT-01 | `packages/ingestion`, source parsing, normalization, canonical jobs and deduplication |
| `jev-selection` | `build/jev-selection` | WT-02 | `packages/scoring`, Jev integration, deciding which jobs to apply for, selection rubrics and evals |
| `candidate-brain` | `build/candidate-brain` | WT-03 | `packages/candidate`, candidate facts, provenance, preferences and resume inventory |
| `application-packets` | `build/application-packets` | WT-04 | `packages/generation`, application answers, tailoring, claim checks and unresolved questions |
| `browser-ats` | `build/browser-ats` | WT-05 + WT-06 | `packages/browser`, `packages/ats`, `adapters`, mock forms, browser execution and ATS adapters |
| `queue-runtime` | `build/queue-runtime` | WT-07 | `workers`, `apps/api`, the application CLI, pipeline orchestration, retries, idempotency and recovery |
| `dashboard` | `build/dashboard` | WT-08 | `apps/web`, `packages/analytics`, job-selection review, application review, progress and outcomes |

Astra keeps the current coordinator worktree for architecture, task assignment, integration and verification. Shared files have one owner; each task gets an explicit file allowlist, including any fixture directories. Root dependency and lockfile changes go through the core owner. Module boundaries and CLI entrypoint location are fixed during the core task before parallel edits begin.

## Start order

1. **Core first:** establish the repository scaffold, canonical Python/TypeScript contracts, state transitions, event and queue contracts, test commands, and `CONTRACTS.md`. Astra reviews this foundation before other implementations depend on it.
2. **Parallel work against approved contracts:** ingestion, Jev selection, candidate data, browser runtime, queue/API/CLI runtime, and dashboard can develop against shared fixtures. Dashboard integration still requires the runtime API.
3. **Dependent work:** packets consume candidate and selection contracts; the browser worker adds adapters after the generic runtime passes the mock-form acceptance test. Workers may prepare fixtures earlier, but must not invent competing interfaces.
4. **First vertical slice:** ingestion -> Jev selects APPLY -> candidate/packet -> browser -> persisted result -> dashboard. Runtime owns connecting the executable pipeline; Astra owns the integration check. Also verify SKIP and REVIEW stop before application preparation.

All eight workspaces may exist from the start. Astra dispatches only tasks whose required contracts and inputs are ready. Tests and infrastructure must use workspace-specific ports, databases, queue namespaces and browser profiles where relevant; workers must not share mutable test state.

## Superset control protocol

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

Each assignment includes its objective, approved contract/base commit, allowed files, dependencies, acceptance criteria, verification commands and completion format. Workers return the architecture's completion receipt and the `SUPERSET_WORKER_DONE` or `SUPERSET_WORKER_BLOCKED` marker. Astra checks the reported diff and tests before integration, records the exact result commit, and sends dependency updates to the affected workers.

Recover a lost terminal mapping with `terminals list`. Where the provider session ID is known, `agents create --resume-session` can restore its conversation. Verify the recipient and model after recovery. Do not launch a duplicate writer just because a screen is quiet. Completed implementation terminals remain available for inspection; only task-owned temporary probe terminals are disposable during setup.
