# Measured lessons from the 500-job run

These are historical observations from September 22–23, 2026, not current machine settings or universal limits. The search completed with 500 new Saved cards and 600 total, an unchanged 118-entry baseline, and no unresolved critical audit findings. Its timer was **79m 05s**, which missed the requested 44-minute total-time benchmark. Saved leads retained explicit pay and availability caveats.

## What changed our decisions

| Observation | Reusable change | Evidence / limitation |
|---|---|---|
| Two reviewers lagged behind 26 finders. | Move low-yield or exhausted producers into independent review; hand over exact claims and teach the writer the new ownership map. | The run moved to 22 finders, six reviewers, one writer, and one coordinator. This is a case-specific allocation, not a recommended fixed pool. |
| Eight or nine simultaneous foreground native opens coincided with 90-second timeouts and dropped bridge connections. | Separate browser capacity from model count, lease sessions, and use a shared queue; use permitted public routes after evidenced failures. | A later single open took 6.2s. No optimal universal slot count was established. |
| Superset's model whitelist rejected a model the installed Claude runtime could use. | Verify model support at the actual runtime; use the supported terminal launch path and record the resolved model. | Terminal creation returned `terminalId`. This does not establish process liveness or model completion. |
| Large tool payloads slowed the notification hook; a 2.5 MB payload took about 2.7s to parse in one observation. | Keep full payloads in private files and return small summaries. Cache schemas once, preserve required hooks. | One visible hook delay exceeded two minutes under load; payload size was not the only load source. |
| The 24 GB host reached about 19 GB of swap usage with many agent/browser processes. | Measure current pressure and useful yield; stop surplus discovery and completed children, cap expensive operations, and release only owned completed resources. | Cumulative swap and summed RSS are not proof that every process is stale or a measurement of unique physical RAM. No percentage memory saving was measured. |
| A large save plan held the run lock for roughly 27 minutes; verified counts looked stale. | Use smaller named-file plans, publish aged verified checkpoints, and wait on the exact operation. | About 50 SAVE actions was a useful subsequent batch size; correct throughput depends on the host and evidence. |
| The writer's `pgrep -f` wait matched the shell containing that same text. | Wait by the original tool handle or exact PID/start identity and verify completion. | The helper really was still running in this case; an observation bug did not justify restarting it. |
| Six durable writes per save repeatedly serialized a growing ledger. | Remove redundant audit copies from future writes while retaining full evidence in hash-pinned plans and keeping every checkpoint. | The reviewed helper change passed 43 tests; new non-SAVE references averaged about 444 bytes versus 10,935 bytes for full records. No full new-run speed measurement exists yet. |
| Compact formatting alone made a 19.44 MB ledger 17.82 MB (8.3% smaller). | Measure before claiming speed improvements. | Write+fsync medians showed no measurable improvement under load. A separate archived-compaction prototype was not applied to the completed live run. |
| A projected pool exceeded the target while finders were still expanding sources. | Stop discovery at a batch boundary and use a modest replacement buffer. Complete only concrete remaining checks. | The estimate remained an upper bound until alias, eligibility, live Saved, and baseline checks. Do not report a projected pool as completed jobs. |
| One retitled listing duplicated a previously closed application; two listings had expired source dates. | Deduplicate across all lanes, apply authoritative corrections before counting, and replace disqualified run-created cards. | The original baseline stayed untouched. An expired marketplace date does not prove the employer definitely closed the vacancy. |
| The first dashboard snapshot was loading; an older populated view did not reflect later saves. | Reload, wait for populated state, capture genuine fresh UI evidence to a file, and inspect the Saved heading/count before finishing. | Raw snapshot output can exceed model context budgets. Do not substitute a hand-written count. |

## What ships and what remains unmeasured

The skills preserve small task briefs, bounded queues, explicit ownership, small status outputs, conservative resource inspection, and recoverable save batches. The helper already preserves mixed old/new ledger formats and full plan evidence. These changes are verified operational behavior or instructions; they are not a promise of a particular runtime, RAM reduction, or provider bill.

On the next comparable authorized run, record target/scope, host capacity, useful candidates per stage, peak browser concurrency, current pressure samples, serialization/batch times, and final verified elapsed time. Change one bottleneck at a time when possible. Avoid repeating a heavy benchmark during production merely to improve confidence in a minor formatting change.

## Provenance

The original checkout retained private evidence under `.imx/find-500/`: `final-receipt.json`, `coordination/AUDIT_COMPLETE.json`, `coordination/review-assignments.json`, `coordination/audit-assignments.json`, `workers/w25/ledger-compact-receipt.json`, `workers/w25/ledger-step23-receipt.json`, `workers/w30/verdict-ledger-step23.json`, and `ui/final-snapshot.txt`. Those are historical provenance paths, not prerequisites or reusable run IDs. No future worker should load the old run or personal tracker data to use these skills.
