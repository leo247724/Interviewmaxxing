---
name: find-jobs-fast-batches
description: Reduce repeated parsing, oversized output, and browser contention while collecting or reviewing Interviewmaxxing jobs. Use when concurrent source workers or large tool payloads slow a search.
---

# Fast job batches

Use alongside find-jobs-source or review-job-candidates. Keep the current role, location, attendance, pay, evidence, and duplicate rules. Historical failures are not current transport state.

1. **Keep output and context small.** Write bulky JSON, HTML, schemas, SQLite exports, browser snapshots, and complete receipts into private worker files. Print a short summary, concrete error, and artifact path. Cache a schema once per relevant version, then query only needed fields. Keep permissions and hooks; reduce payloads rather than disabling them.
2. **Budget the shared browser bridge.** Verify current health and follow the task's browser/session priority. A healthy bridge does not prove sign-in. Give each worker an exclusive session/source lease; another worktree alone does not isolate native fixed-source sessions. If a current attempt fails, preserve the session, error, and any staged evidence. Let an in-flight call reach a known result before retrying. Use a bounded recovery attempt when it can resolve the observed problem; after actual transport/access failure, use the permitted public source/employer routes. Never bypass sign-in/challenges or use private LinkedIn/Voyager APIs.
3. **Queue only a verified owned wrapper.** The included `queued_search.py` executes `RUN/owned_search.py`; it does not create or prove that wrapper's isolation. Use it only when the coordinator supplied and checked that run-specific wrapper. Otherwise serialize the native fixed-source session using the supported route. All workers on the shared bridge must honor one agreed budget; separate run directories or different `--slots` values do not form a common cap. Select slots from observed bridge capacity, not model-worker count. The wrapper has no wait deadline, so the owner must retain its handle and bound the task's wait/recovery; do not leave abandoned queues.

   `"$imx_python" <skill>/scripts/queued_search.py --run-dir "$imx_run_dir" --slots "$imx_browser_slots" -- --worker wNN search --sources SRC --query-file Q --profile "$imx_profile" --window foreground --limit 120 --details 40 --max-pages 3`

   Use the current run's remote/onsite query policy. Do not copy a historical `--no-remote` flag into a remote search.
4. **Deduplicate before details.** Check source IDs, normalized URLs, employer requisitions, and company/title matches against the current all-lane pipeline index and other workers' delivered-but-unsaved batches. One coordinator maintains a compact identity/claim list for staged work; reviewers settle overlaps using the named evidence files. The checker below covers the pipeline index and the input files supplied together; it does not discover other workers' pending files. Ambiguous aliases need identity review, not a guessed new ID. Re-filter already-fetched evidence before expanding search queries.
5. **Deliver small immutable batches.** Aim for the first 5–10 complete candidates, then roughly 10–20 per batch as yield permits. Write atomically, then emit a same-stem `.ready` marker. Corrections use a new file and identify what they supersede. Do not edit a delivered artifact.
6. **Preflight before `.ready`.** Run the read-only checker on each finished candidate batch:

   `"$imx_python" <skill>/scripts/check_batch.py --run-dir "$imx_run_dir" "$imx_candidate_file"`

   Fix invalid objects and proposed saves that duplicate the index or batch before publishing. After a successful save, the refreshed index will correctly identify those rows as existing. A clean preflight is structural evidence; independent semantic review still decides.
7. **Reuse full evidence, summarize status.** Retain raw pages/API JSON privately. Evidence strings contain the source URL, real observation time, and relevant duties/attendance/pay wording. Keep estimated pay separate from employer numeric bounds. Publish small status updates after batches or meaningful events; do not reread the entire ledger or reconstruct the schema on each poll.

For role handoffs and stopping surplus work, use [coordinate-worker-pool](../coordinate-worker-pool/SKILL.md). For host measurements, use [worker-resource-budget](../worker-resource-budget/SKILL.md). Read [the measured lessons](../coordinate-worker-pool/references/lessons.md) only when diagnosing a corresponding bottleneck.
