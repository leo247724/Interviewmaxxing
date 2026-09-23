# benchmarks/performance — offline performance harness (task P0)

Standard-library Python (3.11+). Fictional data only. No browser, no network, no
provider calls, no private profile, no `env.local`. Load is bounded: single process,
four worker threads plus at most four heartbeat threads in the chaos test,
40 items per variant (20-second cap each). Seeded virtual runs use no real waits.

```bash
cd benchmarks/performance
python3 -m imx_perf all                 # fixtures + funnel + selection + chaos + pipeline
python3 -m imx_perf funnel              # capacity requirements per tier
python3 -m imx_perf selection           # Jev call strategies, cache/invalidation
python3 -m imx_perf chaos               # durable-queue prototype under random worker kills
python3 -m imx_perf pipeline [--quick]  # discrete-event pipeline simulation
python3 -m unittest discover -s tests -t .
```

Measured mode times the real packages on temporary databases. Run it with an
interpreter where they import (never edit that venv; it belongs to another worker):

```bash
/Users/leo/.superset/worktrees/Interviewmaxxing/build/queue-runtime/.venv-task/bin/python \
  -m imx_perf measure --tag queue-runtime
```

Outputs go to `results/` (JSON plus Markdown tables). `docs/performance/benchmarks.md`
summarizes the committed results and their limitations; `docs/performance/architecture.md`
is the design they support.

## Layout

| Path | What |
| --- | --- |
| `imx_perf/config.py` | `Assumptions` with a provenance label per field (observed / modeled / external) |
| `imx_perf/fixtures.py` | seeded fictional listing population, question catalog, candidate evidence |
| `imx_perf/funnel.py` | closed-form requirements per tier (Little's law, lane sizing, cost) |
| `imx_perf/selection_sim.py` | J2 policy mirror + stub judge; call strategies; cache scenarios |
| `imx_perf/durable_queue.py` | Phase 1 scheduler prototype: leases, lanes, backpressure, budgets, effect ledger |
| `imx_perf/chaos.py` | kill-workers-randomly test over the prototype |
| `imx_perf/pipeline_sim.py` | virtual-clock simulation of the seven stages with resource contention |
| `imx_perf/measure.py` | optional timings of the real packages (temporary `IMX_HOME`) |
| `fixtures/` | generated fictional fixtures and the default assumptions file |
| `results/` | committed outputs of the runs described in `docs/performance/benchmarks.md` |
| `tests/` | unittest suite for the queue invariants, the models and the fixtures |

## What the numbers mean

* **observed**: historical code/provider observations (provenance in `config.py`),
  distinguished from local measurements in the report. These are not live quota facts.
* **modeled**: computed from stated assumptions; edit `fixtures/assumptions.default.json`
  and pass `--assumptions` to see the effect.
* **external**: market supply, site behaviour, provider limits; not measured.

The stub judge in `selection_sim` answers from fixture truth plus noise. It exists to
count calls, bytes and latency under each strategy and to exercise the policy mirror.
It says nothing about Jev's accuracy; that needs candidate-labeled fixtures and real,
bounded, paid calls, which are out of scope for this task.

## Reproducibility and limits

The committed run uses `python3 -m imx_perf all --seed 42 --population 2000`.
Fixtures and virtual selection/pipeline numbers are seeded. Thread scheduling, UUIDs,
local timings and run metadata are nondeterministic. Reports include the command,
interpreter, checkout and a SHA-256 over harness modules so an uncommitted source
snapshot is distinguishable from its base commit. Optional measurement records the
actual imported package paths/checkouts, which can differ from this worktree.

`chaos` uses cooperative abandonment and a local SQLite effect ledger. It tests
at-most-once fictional dispatch, intent-before-effect uncertainty and reconciliation
routing; it does not prove exactly-once website delivery, production integration,
OS-process-kill recovery or power-loss durability. Queue tests separately cover
full task identity, leases, fairness, backpressure, cost reservations and shared
cooldown. The provider reservation prototype is not wired into the throughput stub;
its safe accounting is tested independently. Current two-stage Jev behavior must
remain until an authorized held-out quality evaluation supports a change.
