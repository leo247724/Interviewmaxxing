# P0 benchmark receipt

All fixtures are invented. No browser, employer site, private candidate/profile, paid provider, production database or new dependency was used. This receipt does not establish site-confirmed throughput.

## Commands and environment

From `benchmarks/performance`:

```sh
python3 -m unittest discover -s tests -t .
python3 -m imx_perf all --seed 42 --population 2000
python3 -m imx_perf chaos --seed 42  # rerun after cross-kind admission fix
python3 -m imx_perf fixtures --seed 42 --population 2000
python3 -m imx_perf selection --seed 42 --population 2000  # rerun after cache-admission correction
/Users/leo/.superset/worktrees/Interviewmaxxing/build/queue-runtime/.venv-task/bin/python -m imx_perf measure --tag queue-runtime
```

29 unit tests pass. The seeded suite runs 2,000 fictional listings, three 40-item worker-abandonment variants and 12 virtual pipeline scenarios. Local worker scheduling is nondeterministic. Each result JSON records argv, interpreter, platform, base checkout and harness module SHA-256; repo_head is the pre-commit base, not a claim the generated files already existed at that commit.

Offline suite interpreter: 3.14.2; macOS-15.7.4-arm64-arm-64bit-Mach-O. Optional package timing interpreter: 3.12.13; imported packages from queue-runtime checkout `05c87b6`. This is not a timing of every sibling's latest source.

## Locally measured prototype invariants

| Variant | Abandonments | Lease losses | Reconcile routes | No effect after intent | Effects | Result |
| --- | --- | --- | --- | --- | --- | --- |
| default_heartbeat | 11 | 0 | 4 | 3 | 37 | PASS |
| long_steps_heartbeat | 6 | 0 | 3 | 3 | 37 | PASS |
| long_steps_no_heartbeat | 5 | 55 | 40 | 39 | 1 | PASS |

Each run ended with 40 DONE scheduler dispositions, zero live leases, zero duplicate ledger effects and zero sampled lane violations. DONE may mean routed/reconciled with no fictional effect; it does not mean an accepted application. Without heartbeat, long work is fenced and routes to uncertainty rather than being counted as successful throughput. The local effect ledger cannot prove exactly-once remote delivery. Worker abandonment is cooperative, not an OS process kill or power-loss test.

Tests also exercise cross-kind application admission coalescing, seven-field task identity, stale-owner/token/expiry fencing, retry timing and dead letters, high-water admission, restart-visible uncertainty, aged reconciliation priority, conservative reservations before paid attempts, timeout charges, and shared hour-long Retry-After that a shorter cooldown cannot overwrite. Cache admission requires a completed final decision and excludes final-request provider failures even when the first request succeeded (regression: 31 valid caches rather than 66 partial/complete flows). Same-call independent final answers cannot read sibling answers; failed attempts still consume modeled bytes/time; exhausted submit failures never become acceptance.

## Local real-package microbenchmarks

| Stored rows after dedupe | list(limit=50) median ms | Ranked list median ms |
| --- | --- | --- |
| 990 | 10.472 | 49.258 |
| 4961 | 65.159 | 268.37 |

Five samples per listing path. These are JobStore calls on temporary databases with seeded fixture descriptions, not HTTP GET timings. The reviewer separately measured 1k/5k/10k rows at 14.05/81.49/158.67 ms with uniform 984-character text; those different-workload relay numbers are documented in architecture.md. Current service batching and indexed pagination need their own before/after verification.

The sum of median canonical application-store operations is 0.647 ms (not end-to-end browser time). Sixty full-description selection inputs took a median 1.426 ms locally with 84 **fake transport** calls; code-gated inputs require zero calls. Repeating inputs made 0 calls. These mixed local timings say nothing about live Jev latency. Raw samples/statistics are in measure.queue-runtime.json.

## Modeled demand, not achievable capacity

Defaults: .85 direct acceptance yield, .70 utilization; submit weighted mean 135s, 30% input blocks × 5min, resume 45s and two 45s checks for each 5% unknown. One browser lane remains the runtime default.

| Target accepted/day | Attempts | Browser hours incl resume/reconcile | Human hours | Total lanes / 24h at 70% | Total lanes / 8h at 70% |
| --- | --- | --- | --- | --- | --- |
| 1000 | 1176 | 50.0 | 29.4 | 3 | 9 |
| 5000 | 5882 | 250.0 | 147.1 | 15 | 45 |
| 10000 | 11765 | 500.0 | 294.1 | 30 | 90 |

Lane counts are arithmetic requirements. They do not authorize opening those lanes or show laptop/browser/site support. Additional pacing, challenge and queue overhead may increase demand. The funnel JSON separates submission, resume and reconciliation hours; its sensitivity lanes cover submission only. Supply, site acceptance and provider quota limits remain external and unmeasured.

## Selection and pipeline mechanics

| Strategy | Successful stub calls | All attempts | Estimated input tokens | Stub mean / p95 seconds |
| --- | --- | --- | --- | --- |
| baseline_two_call | 2738 | 2793 | 4977896 | 1.109 / 1.802 |
| gate_then_two_call | 248 | 251 | 684534 | 1.067 / 1.743 |
| gate_compact_final | 248 | 251 | 549238 | 1.067 / 1.743 |
| gate_combined_selective | 186 | 189 | 599058 | 0.798 / 1.5 |
| gate_single_request | 124 | 125 | 452376 | 0.513 / 0.923 |

These are seeded assumptions, not model quality results. Per-attempt latency uses a fictional lognormal median `t_jev_call_s` with fixed sigma .35; p95 is a sampled output, not a supported configurable parameter. Hold-gating defers 1,245 listings; call savings do not imply an increased qualified supply. Byte/4 token estimates charge all failed/retried attempts. Separate queue tests enforce conservative provider reservation/cooldown; that budget prototype is not integrated into the virtual throughput provider. Keep the two-stage production decision until an authorized held-out quality comparison supports changing it.

The twelve pipeline scenarios report observations, unique listings, APPLY, attempts, simulated direct acceptance, reconciliation, human/browser hours, resource utilization and stage p50/p95. `unresolved_at_horizon` includes work left queued, blocked, failed or uncertain. The highest utilization is only the busiest resource under this scenario, not proof of a real bottleneck.

Pipeline limitations: generated source populations and detail success are approximations, not a faithful replay of J1; scenario scheduling changes fixture/random-number order, so differences between lane counts are not causal estimates; tenant spacing conservatively occupies the modeled browser; latency percentiles include queueing only for completed stages and omit censored in-flight work. Human answer reuse is fictional and not production authorization logic. Four-lane rows are explicitly experimental sensitivity runs. Screens and interviews are null. No site acceptance was observed.

Artifacts: [architecture.md](architecture.md), [harness README](../../benchmarks/performance/README.md), [funnel](../../benchmarks/performance/results/funnel.md), [selection](../../benchmarks/performance/results/selection.md), [queue](../../benchmarks/performance/results/chaos.md), [pipeline](../../benchmarks/performance/results/pipeline.md).
