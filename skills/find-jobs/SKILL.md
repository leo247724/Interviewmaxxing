---
name: find-jobs
description: Coordinate a timed Interviewmaxxing job search, review candidates, and save a requested number of distinct matching jobs to the local pipeline. Use for find-and-save batches or resuming an unfinished search across LinkedIn, Google Jobs, Built In, and employer sites. Does not submit applications or handle assessments.
---

# Find and save jobs

Turn the user's search into a verified set of Saved cards. Use the existing Interviewmaxxing checkout and its configured local service. Keep search evidence separate from semantic approval and pipeline writes.

## Establish the run

1. Carry forward the user's current role, geography, attendance, compensation, target, and save authority. Ask only about a missing constraint that materially changes the result. Title phrases are search seeds, not an exact-title allowlist. Do not carry an old Austin-only or remote policy into a different request.
2. Resolve the checkout, working Python environment, canonical jobs **database file**, service URL, dashboard origin, and connected browser profile from current configuration. Do not print credentials or reuse historical profile, target, run, or listing IDs. Verify imports and `python -m interviewmaxxing_jobs search --help` before using changed CLI versions.
3. For a resume, read the existing `run.json` and continue its timer, baseline, and ledger. For a new request, select a new private, git-ignored run directory. Never reuse a completed run directory. The [Austin scope](assets/austin-performance.scope.json) and [query](assets/austin-performance.query.json) are examples of the earlier request, not universal defaults.
4. Read [helper commands](references/helper-commands.md). Initialize the run before acquisition, then build its duplicate index. `init` is idempotent for identical arguments; it must not reset the timer or baseline. `index` includes all existing pipeline lanes, including Closed.

## Search and review concurrently

5. Delegate one source at a time to each worker using [find-jobs-source](../find-jobs-source/SKILL.md). LinkedIn, Google Jobs, and Built In can run concurrently. Give each worker the run directory, exact scope/query, live browser profile, exclusive source lease, separate staging database, current duplicate index, and a bounded acquisition budget. Native sessions are fixed per source; another worktree does not isolate the same source session.
6. Keep one coordinator as the pipeline writer. Source workers only collect; reviewers only write reviewed artifacts. Hand small completed batches to [review-job-candidates](../review-job-candidates/SKILL.md) while acquisition continues. Start with batches of roughly 10–20 candidates and adjust to observed yield. Do not wait for every source to finish.
7. Check IDs, employer requisitions, and known duplicates before expensive description retrieval. Feed accepted IDs and duplicate/exclusion reasons back to workers after each batch. When new eligible yield falls, change search seeds or verify employer postings instead of repeatedly fetching the same cards. Preserve source failures and the exact sign-in remediation; an empty blocked source is not a successful search.

For a large worker fleet with browser contention or oversized tool output, use [find-jobs-fast-batches](../find-jobs-fast-batches/SKILL.md). Keep evidence and eligibility checks intact while reducing repeated parsing, schema dumps, and concurrent browser opens.

When managing several workers, use [coordinate-worker-pool](../coordinate-worker-pool/SKILL.md) for bounded assignments, queue handoffs, and stopping surplus acquisition. The sole writer can use [find-jobs-save-batches](../find-jobs-save-batches/SKILL.md) for manageable plans and accurate progress. Load [worker-resource-budget](../worker-resource-budget/SKILL.md) only when choosing resource limits or investigating host pressure.

## Save, reconcile, and finish

8. Accept only explicitly reviewed envelopes from the reviewer. Run `preview` on named files, inspect its exclusions, uncertainty notes, duplicate holds, and target capacity, then `save` the hashed plan within the user's existing find-and-save authority. Preview is a concrete validation step, not an additional user approval requirement. Never import collector booleans, wildcards, or provisional rows.
9. Use `report` after each batch. Count only canonical listings owned by this run that are currently in Saved. Observations, approved rows, aliases, Closed cards, historical receipts, and unrelated concurrent additions do not count. Existing tracker entries must remain unchanged. Do not replay a failed write blindly; resume through the helper's ledger and fresh revisions.
10. Continue until the requested count is verified, retaining the original scope. If supply or access prevents completion, report the actual shortfall and keep the run unfinished; do not stop the timer or weaken eligibility to reach a number. A user's explicit cancellation takes precedence.
11. Reload the correct dashboard, wait for the populated pipeline, and capture fresh observable UI evidence. Follow the workspace's browser-tool priority for dashboard testing. Run `finish` with that snapshot, observation time, and actual URL. Completion requires the target in the service, an unchanged baseline, and the correct total Saved count in the UI; existing Saved cards may make that UI total larger than this run's target.
12. Return the newly Saved count, elapsed time, dashboard link, and material pay, availability, or description-review counts. Retain the private receipt and source evidence. Release only owned source sessions, preserving sessions needing user sign-in. Record new actionable source patterns in the project runbook without copying personal tracker data.

## Evidence and scope rules

- Review responsibilities, office attendance, pay, availability, and identity separately. Primary employer contradictions outweigh marketplace cards. Use the review skill for the actual decisions; the helper validates structure and deterministic constraints, not the truth of a job's responsibilities.
- Under `review_uncertain`, unknown pay and ranges crossing the floor may be saved with visible review notes. Under `confirmed_floor`, only comparable employer-published lower bounds at or above the floor qualify. Never call every saved role salary-confirmed.
- A newer search timestamp does not prove an active vacancy. Do not save known-closed jobs. Unknown availability is eligible only if the run's policy allows review and the evidence is labeled honestly.
- This workflow saves local tracker cards. Application creation, form filling, resume uploads, outbound messages, and assessment responses are separate tasks and are not part of these commands.
