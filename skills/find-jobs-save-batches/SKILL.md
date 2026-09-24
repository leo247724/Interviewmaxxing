---
name: find-jobs-save-batches
description: Run the sole writer for reviewed Interviewmaxxing job batches with bounded plans, recoverable writes, and accurate progress. Use when draining reviewed candidates, reconciling an interrupted save, or finishing a timed search.
---

# Save reviewed job batches

This is the writer procedure within [find-jobs](../find-jobs/SKILL.md). It does not acquire jobs, perform applications, or replace independent semantic review. Use the installed helper and the existing run directory; keep the original timer, scope, target, and baseline.

## Admit a concrete batch

- Read the current reviewer ownership map and explicitly released immutable files. Require matching run ID, named reviewer, original evidence, and `.ready`/hash agreement. Apply newer correction decisions before selecting work. A stale pre-handoff review needs an explicit recorded exception, not a guessed owner.
- Group named reviewed files into a plan. About 25–50 proposed SAVE actions is a useful starting range; tune it to measured commit time and contention, not a universal limit. Corrections, HOLD, EXCLUDE, and RESUME actions are not new saves.
- Run helper `preview` on those exact files. Inspect plan actions, capacity, duplicate holds, scope/uncertainty flags, and provenance. Independent reviewers own the full semantic assessment; investigate contradictions and unresolved flags instead of repeating an entire third review of every unflagged row. Structural success alone never proves fit.
- If the proposed batch is too large, preview fewer intact files. Never truncate or edit a hashed plan. If one reviewed file is itself too large, have its reviewer issue smaller immutable replacement envelopes and record which file they supersede.

Use [the helper command reference](../find-jobs/references/helper-commands.md) for syntax and recovery semantics. Only this writer calls `save`; file collectors and reviewers do not upsert, track, or move cards.

## Commit and report without flooding context

Run `save` with the exact returned plan path. Capture stdout/stderr to private files rather than returning complete native listings or hundreds of receipt IDs. After a batch, call `report`, refresh the duplicate index, and publish only the verified new Saved count, total Saved, actual timestamp, pending intents, validation errors, and uncertainty counts. Store the full result as evidence.

Large plans hold the run lock and delay ordinary reports. While a save runs, label the last verified count with its age and retain the running handle. Do not turn a lock error into zero, reset the timer, or restart the save because a status read timed out. Avoid polling the full ledger: a single coordinator may inspect it if necessary, but ledger-derived progress is provisional until live API/baseline reconciliation. Use real timestamps, never placeholder or estimated observation times.

## Recover and correct

- Wait on the exact task handle or verified PID/start identity. A broad `pgrep -f` wait can match itself. Read the original operation's completion before launching a retry.
- On interruption, inspect `report` and the error, then resume the unchanged plan in the same run. The helper distinguishes acknowledged ownership from an ambiguous lost response. Never claim or edit a matching external card merely to recover the count.
- Scope/config changes or changed reviewed input require a fresh preview. Do not mark a rejected or stale plan processed. A source's proposed SAVE cannot override a later authoritative HOLD/EXCLUDE.
- For a newly disqualified tracked card, verify that this run created it and use a separate authorized pipeline correction with precise evidence. Preserve baseline cards, then add a reviewed replacement. Do not edit the ledger to hide it or create Closed cards for ordinary rejected candidates.

## Preserve cheap, durable evidence

The helper stores new non-SAVE decisions as references into full hash-pinned plans and removes pre-track card lists only after verified saves. Keep plans and reviewed inputs intact. Old full-record ledgers remain readable. Do not remove checkpoints, fsync, ownership evidence, or baseline checks to gain speed. Changing a live ledger layout needs a separately tested, recoverable change within the task's authorization; never prune it during a save. Smaller JSON by itself did not improve write time in the measured run.

## Finish the actual target

Count only distinct canonical run-owned cards currently in Saved. Process known critical contradictions before completion. Read the actual service and unchanged baseline; reload the dashboard and wait until populated. Capture a genuine fresh snapshot to a private file, inspect its URL/title and Saved count, and pass its actual observation time to helper `finish`. Redirect large browser snapshots to disk; a stale loading screen or a manually written count is not UI evidence.

Stop the timer only when the requested target is verified. Report the real elapsed time and missed benchmark, plus material compensation/availability caveats. Release this run's watchers and owned resources; retain the completion receipt and audit inputs.
