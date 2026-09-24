---
name: coordinate-worker-pool
description: Coordinate an authorized pool of Codex or Claude workers with bounded assignments, explicit ownership, and balanced producer/reviewer queues. Use for parallel research or implementation when coordinating workers is part of the task.
---

# Coordinate a worker pool

Allocate work to the current bottleneck. Keep the user's worker/model choices and task scope; a large requested pool does not grant an unlimited budget for nested agents, browser opens, or indexing.

## Start with a small control surface

- Keep one task directory with the objective, scope, worker assignments, artifact paths, and completion criteria. Reuse the existing brief at handoffs. Do not clone the entire conversation, architecture, schema, and raw results into every prompt.
- Use [the worker brief](assets/worker-brief.md) for a bounded package. An implementation worker may write only its assigned files; an independent reviewer assesses other workers' artifacts. Sharing a checkout works for disjoint research outputs. Use isolated worktrees when code edits overlap, with one integration owner.
- Record each worker's requested/resolved model, workspace, terminal or task handle, role, allowed outputs, and source/browser lease. PID and process start time are useful identity evidence when available; a terminal ID is not a PID.
- Use the requested orchestration tool. For Superset launches or model-registry mismatches, read [Superset operation](references/superset.md). Verify actual worker output rather than assuming a created terminal is working.
- One owner handles shared indexes, caches, and schemas. Follow repository graph-first discovery rules; a missing graph is not a reason for every worker to start a full reindex. Scope MCP/tools to the task only where workspace instructions permit; keep required authentication, permissions, and hooks.

## Budget independent resources

Treat logical workers, active model calls, native browser slots, API concurrency, and disk-heavy tasks as separate limits. Preserve an explicitly requested concurrent worker count; bound their expensive tools and work packages instead of silently changing that request. No worker spawns additional agents outside its assigned pool budget.

Use [worker-resource-budget](../worker-resource-budget/SKILL.md) when host capacity is uncertain or performance deteriorates. A high RSS total or existing swap alone does not prove present pressure. Compare completed work, current load/pressure, and repeated cheap samples before changing concurrency. Do not launch more copies of a blocked shared operation.

## Keep stages moving

- Give source workers disjoint queries/boards or implementation workers disjoint files. For job searches, keep semantic reviewers independent and exactly one canonical writer. Use [find-jobs-save-batches](../find-jobs-save-batches/SKILL.md) for that writer.
- Track completed, accepted artifacts and useful yield over short comparable intervals. Raw `.ready` counts include corrections and do not establish job counts. A heartbeat is not proof of useful progress; a stale heartbeat or observation timeout is not proof of a dead worker.
- If review backlog grows while marginal acquisition yield falls, reassign an exhausted or low-yield producer to independent review. Finish or explicitly release its current claim first. Record the new owner and exact in-flight/pre-handoff file hashes, get writer acceptance, then release the queue. Never self-review or silently reuse an old ownership map.
- Treat schema/tooling work as shared work once, not one rewrite per worker. Reuse already-fetched evidence and the current duplicate index before expanding searches. For job batches, keep one compact identity/claim list for delivered-but-unsaved artifacts so reviewers can settle cross-lane overlaps without every worker rereading all raw evidence.
- Stop expanding acquisition when the latest independently approved, deduplicated pool covers the remaining target plus a justified replacement buffer. A projected pool is an upper bound, not a completed or guaranteed count. Keep actual writer capacity checks. Assign only concrete remaining checks; do not invent repeated audits to keep workers busy.

## Observe cheaply and finish

Workers should publish a small status after a batch, meaningful event, or bounded interval: actual UTC time, phase, completed artifact/hash, useful counts, current blocker, next action, and active handle. Store full evidence privately; print a short delta and artifact path. Prefer completion events and one coordinator sampler to repeated model turns or large ledger reads in every worker.

Wait on the original task handle or an exact verified process identity. `pgrep -f` can match the shell containing the search text. An observation timeout does not authorize restarting a writer; first establish whether its operation is still running and reconcile its result.

At the real completion condition, stop new assignments, release only owned completed resources, stop task-specific pollers, and retain concise receipts. Preserve unrelated work and sessions awaiting sign-in. Separate the time to reach and verify the target from later documentation work. Record missed benchmarks and unverified claims honestly. Consult [the measured lessons](references/lessons.md) when choosing what to optimize; they are historical evidence, not current machine settings.
