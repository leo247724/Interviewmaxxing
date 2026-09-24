---
name: worker-resource-budget
description: Size and pace parallel agent workers (model sessions, child agents, browser and I/O slots) from current host measurements, and take a read-only snapshot of CPU, memory, swap and worker process footprint. Use when planning or running many concurrent workers, when a host feels saturated, or before adding workers. Measurement only; it never kills, cleans up or reconfigures processes.
---

# Worker resource budget

Keep parallel work fast by spending the machine on useful work: bounded children, shared expensive slots, compact outputs. Decide from **current** measurements and trends, not from a fixed worker count or another machine's history.

## Measure first (read-only)

```bash
python3 <skill-dir>/scripts/resource_snapshot.py                       # host summary
python3 <skill-dir>/scripts/resource_snapshot.py --inventory workers.json --output /private/path/snap.json
```

- Inventory rows: `{"worker_id", "pid", "expected_started_at"}` (ISO-8601 with offset, or epoch). After launch, read that process's own OS start time, for example `LC_ALL=C ps -p PID -o lstart=`, and convert it using the host timezone to ISO with offset or epoch seconds. Do not substitute the coordinator's launch clock: shells and startup delays can differ by seconds. Without start evidence, identity stays `unproven_identity`.
- Statuses: `matched` (PID alive and started in the same whole second; `ps` has 1 s resolution), `start_mismatch` (different recorded start; possible PID reuse or incorrect metadata), `missing`, `unproven_identity`, `duplicate_claim` (two workers claim one PID; neither is trusted), `invalid_entry`. Only `matched` trees enter `task_aggregate`, deduplicated by PID.
- A `matched` PID is identity evidence, **not** ownership. Before any lifecycle action, confirm independently that the process is your task's (its workspace, parent session, assignment).
- `rss_footprint` sums per-process RSS: shared pages are counted repeatedly, so it is neither unique RAM nor proof of pressure.
- One snapshot is a point, not a trend. Current pressure shows as change between snapshots (available memory shrinking, compressed memory or swap use growing) together with symptoms (tool timeouts, stalled hooks). A large but stable swap figure can be left over from earlier work and is not, by itself, current pressure.
- `null` means unknown. Never read it as zero.
- Stdout is one bounded JSON line; the detailed file is written 0600. The script reads only PID, PPID, RSS, %CPU and start time, never command lines or environments.

## Budget the work, not the headcount

- **Respect an explicitly requested worker count.** If the user or coordinator asked for N concurrent model workers, keep N; do not silently shrink it. Budget the expensive shared resources underneath them instead, and report measured trade-offs so the requester can decide.
- **Model scheduling is optional; expensive slots are budgeted.** Where no count was requested, how many model turns run at once is a scheduling choice. Browser/bridge sessions, heavy I/O, large indexing and big file rewrites are the scarce slots: give them explicit small budgets (lock-file slots, a coordinator queue, sequential batches) so logical workers wait briefly instead of contending and timing out.
- **Tune from throughput, latency and pressure trends.** Increase a slot budget while useful throughput (verified results per unit time) rises and per-call latency and timeouts stay flat. Reduce it when throughput flattens or falls, latency or timeouts climb, or pressure is trending up between snapshots. Re-measure after each change; no fixed number transfers between hosts.
- **Bound child agents.** Spawn children only for independent work that pays for its context; cap how many run at once; collect their results into files; stop each child when its result is saved.
- **Schedule by yield, early.** Ship the first small verified batch quickly, then give capacity to lanes that are producing. Reassign workers from exhausted or sustained zero-yield lanes to concrete remaining work; preserve an explicitly requested concurrent worker count until the task's completion condition.
- **Avoid redundant indexing and crawling.** Reuse an existing index, board pull or cached artifact; deduplicate before fetching details; never have several workers rebuild the same index.
- **Compact output and progressive context.** Send bulky JSON/HTML/logs to files and print counts or `head`; read only the reference section you need. Large tool output costs memory, hook time and context for every later call.
- **Release only what you own and finished.** Close your own completed sessions, tabs and children. Preserve unrelated, sign-in-blocked or user-facing sessions. Do not treat an executable name (e.g. every `python`, `node`, `Chrome`) as proof of ownership.

## Out of scope

No killing, renicing, swap purging, reboots, hook bypasses or automatic cleanup. This skill and its script do not change concurrency themselves; they inform the coordinator or user, who decides.

For job-search browser batching specifics (shared OpenCLI bridge, queue wrapper, batch checker), use `find-jobs-fast-batches` when it is installed; this skill does not repeat that workflow.

## Observed history (context, not policy)

On one 14-core, 24 GB macOS host (2026-09-23), ~30 concurrent model workers plus Chrome coincided with load around 36–41, about 19 GB of swap in use, 90-second browser-bridge open timeouts, and multi-second post-tool hooks on large outputs. Output shrinking, queued browser opens and stopping idle children were introduced together during that run without a controlled comparison, so no individual effect or memory saving is established. Treat these as symptoms to watch for, not thresholds.
