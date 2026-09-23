# Performance runtime: P0 design and offline prototype

This is a design and fictional benchmark, not a production scheduler or a claim of
1,000–10,000 site-confirmed applications/day. Production packages are unchanged.
The inherited design checkpoint was `457d8ca`; the parent relayed Astra Max's code
review in `.handoff/astra-performance-review.md`. Results and exact commands are in
[benchmarks.md](benchmarks.md).

## Evidence vocabulary and outcome counters

* **Measured locally:** elapsed time for fictional local operations, or observed
  prototype invariants. This does not measure a provider or employer website.
* **Code observation:** a named checkpoint's behavior; sibling worktrees may change.
* **Modeled:** arithmetic or virtual-clock results under explicit assumptions.
* **External and unmeasured:** qualified listing supply, provider quotas/latency,
  employer acceptance, site challenge thresholds, browser memory/isolation, screens,
  interviews and offers.

Track discovered observations, deduplicated listings, fully qualified APPLY decisions,
packets ready, application attempts, site-observed acceptance, user-reported acceptance,
screens and interviews separately. `SUBMISSION_UNKNOWN` is neither an acceptance nor
permission to resubmit. The simulator's historically named `V5_submitted_site_observed`
field is **simulated acceptance only**; its report and evidence label say so. User
confirmation remains separate in the production contract. Screens/interviews are null
in the model rather than invented conversion rates.

## Current constraints and first fixes

The following are dated code observations from the review relay, not fresh production
measurements. The inherited baseline inspected J1 `1d8f2ee`, J2 `0f58421`, core
`d43ce6b`, browser `983192c`; these are historical checkpoints.

1. The service dispatcher owns one in-memory application slot and refuses concurrent
   runs with 409. Durable task records become INTERRUPTED after restart without lease
   replay. Add a scheduler around the canonical stores; preserve their claim and
   submission authority.
2. The old runner's 300-second claim and 600-second user wait conflict. Reviewer's
   virtual-clock checks at 299/301/599 seconds found expiry after 300 seconds. The
   separate I1R worker owns the production heartbeat fix. This prototype's heartbeat
   is not evidence that the production runner has been repaired.
3. Fix GET-list read amplification before browser pooling. At the reviewed baseline,
   per-card `latest` opens selection storage and parses history; `item_for_listing`
   rescans pipeline storage despite a preloaded map. J1 applies `limit=50` after decoding
   all rows. Reviewer measured fictional 984-character descriptions, five repeats:

   | Stored listings | Decoded despite limit 50 | Median ms |
   | --- | --- | --- |
   | 1,000 | 1,000 | 14.05 |
   | 5,000 | 5,000 | 81.49 |
   | 10,000 | 10,000 | 158.67 |

   Maximum at 10k was 186.74 ms; EXPLAIN showed SCAN and temporary sorting. These are
   relay measurements, not rerun here. Parent reports service `5115dae` batches
   pipeline/decisions and J2 `latest_many` is being added; completion/performance must
   be verified in those owners' receipts. Remaining follow-up: indexed cursor
   pagination, bounded summary queries, and separate detail/history reads. Apply
   Austin preference ranking **before** pagination; a SQL pre-limit must not discard
   higher-ranked eligible jobs. Measure query count and decoded-row count as N grows.
4. J1 sources are sequential, with at most ten detail attempts of fifty cards/source
   by default. LinkedIn detail waits are 3–6 seconds. Forty attempts across four sources
   do not establish forty FULL descriptions. `p_enrich_needed=.80` is a modeled bound,
   not measured enrichment yield. Persist/dedupe cards first, then enrich only missing
   or stale eligible records, under source-local budgets and resumable cursors.

## Scheduler and identity

The SQLite prototype is `benchmarks/performance/imx_perf/durable_queue.py`.
It exercises these contracts without connecting to the production state machine:

* A task has kind, full input key, payload references, available_at, input_version,
  per-source cursor, attempts/max_attempts, owner/token lease, and append-only events.
* Decide identity includes candidate identity, candidate evidence version, canonical
  job identity, job evidence version, preferences, rubric and model version.
  `decision_key` requires all seven. Candidate projections may be cached only by
  verified-fact version and candidate identity.
* Keep a semantic cache hash separate from execution/audit identity. Application URL
  changes may reuse a semantic judgment, but always invalidate/recheck execution URL,
  listing staleness, application-link provenance and candidate/job duplicate status.
  A semantic hit never grants submission authorization.
* `BEGIN IMMEDIATE` serializes admission and claims. High-water backpressure is checked
  in enqueue. All lease mutations fence owner, token and expiry. Retries use persisted
  available_at; safe work reaches DEAD after bounded attempts. No provider failures
  are cached. Reopen the same DB to demonstrate restart visibility.
* Durable submission intent precedes dispatch. Expiry/failure/release after intent
  becomes reconciliation, even when no effect was recorded. New apply admission for
  that key coalesces to the existing intent; reconciliation cannot dispatch an effect.
  Production must consult canonical `begin_submission`, claim and reconciliation
  APIs, including the ambiguous crash-between-click-and-record window. A SQLite
  effect ledger is not exactly-once delivery to a remote website.
* A reconciliation waiting 15 minutes outranks new submissions at the next eligible
  slot. This prevents starvation under ongoing arrivals; it cannot preempt a held
  browser/profile, bypass spacing or guarantee an external completion deadline.
  Production should alert on oldest reconciliation age and bound lease/work duration.
* One process/thread owns each connection. This prototype uses WAL/NORMAL and tests
  cooperative worker abandonment, not OS kill or power-loss durability. Production
  durability requirements remain those of the canonical stores (including FULL sync).

Provider control is separate from task completion. Reserve a conservative cost before
**each** paid attempt; retries receive new attempt IDs. Failed/time-out calls retain
reservations when billing is unknown. Known usage reconciles the reserve; unexpected
cost overages are recorded and prevent further reservations. Shared persistent
cooldown honors the longest Retry-After across workers, including an hour-long value.
A duplicate reservation cannot dispatch again. Budget periods are explicit names
(e.g. provider:2026-09-22); day rollover/adaptive concurrency are future integration,
not capabilities claimed by this prototype. Request token estimates are byte/4, not
tokenizer measurements; enforce a serialized request byte/context bound before a
real call. Source-page budgets apply per source and to all fallback detail loads.

## Jev selection experiments

The baseline is six focused questions in one call, then a final question in a second
call whose state includes those answers. Independent questions in one request see
**the same input state**; a sibling final question cannot consume answers from that
request. No cross-job batch API is assumed.

The offline harness compares baseline two-call, hold-gated two-call, compact second
state, independent one-call final, and independent first-pass final with selective
second-call escalation. The stub's independent final reads fixture truth, not focused
answers. Stable per-listing random seeds reduce accidental disagreement from gating.
These numbers compare mechanics and invented noise; they measure no Jev accuracy.

Before changing the two-stage semantics: freeze a held-out, candidate-labeled fixture
set with evidence completeness, role match, qualifications, ambiguity and injection
cases; compare precision/recall for APPLY, false APPLY severity, REVIEW/abstention,
disagreement by segment, latency, all paid attempts, bytes and cost. Require no loss
of safety/quality gates. Retain model and rubric versions on every result and cache
entry. Paid evaluation needs separate authorization and is not performed here.

First save unnecessary work: hard constraints skip the provider; irreparable review
holds stop before calls; curable holds route to enrichment. Do not convert REVIEW to
APPLY to raise throughput. Candidate verified facts and full job evidence remain
required. Provider pricing cited by the inherited review ($0.042/M input, $0 output)
is a dated assumption, not a quota or measured latency. Vendor triage latency is a
different workload; there is no measured Jev p95 for this selection flow.

## Browser and human work

Default **one browser lane**, one action per session/profile. Additional lanes are
experimental until tab ownership, memory, isolation and shared Bridge contention are
measured under separately authorized bounded load. No site limits are bypassed.

A future warm Chromium process must create a **fresh Page and fresh
GenericApplicationBrowser for each application**, then dispose them; reusing the
runtime object carries `_accepted`, `_pending` and `_filled` state. Profile lock,
one active run per profile, ATS tenant serialization and canonical store claims all
remain necessary. Human waits release scheduler resources where the canonical runner
permits; heartbeat must cover any still-held claim. Reconciliation shares the lane
with bounded-age priority, never an automatic resubmit.

Missing explicit answers hold for the user. Reuse only within the user's actual
application/job/global answer scope and question fingerprint. New wording is new
input; normalization alone must not authorize reuse. CAPTCHA/sign-in/challenge pages
wait for the user. No inferred consent, invented qualifications or hidden mass runs.

## Capacity arithmetic and next measurements

For 1k/5k/10k completions in 24h, required completions/minute are
0.694/3.472/6.944 and completion budgets are 86.4/17.28/8.64 seconds. At an 8h horizon,
the budgets become 28.8/5.76/2.88 seconds. These are demand targets, not observations.

The funnel defaults to .85 direct acceptance yield (1 − .05 unknown − .10 failure),
70% utilization, 30% input blocks at five minutes each, a 135-second weighted submit,
45-second resume and two 45-second checks per unknown. It reports upstream supply,
provider demand, separate submit/resume/reconciliation browser hours, human hours and
lanes for 24h/8h horizons. Submission-only sensitivity lanes intentionally exclude
resume/reconciliation; total lanes are a separate field. Neither includes all external
queuing/pacing/challenge costs. At 10% blocks × five minutes, 1k attempts alone needs
8.3 human hours; 10k needs 83.3 hours. At the .30 default the demand is higher.

A selector with a hypothetical 0.5–2 second select at 70% utilization has arithmetic
capacity 121k–30k selects/day. It is not the bottleneck **only under those assumptions**.
Browser hours, human availability and qualified supply may dominate; site challenge
thresholds and acceptance yield remain unmeasured. Multi-lane simulation is sensitivity
analysis, not permission or evidence to open those lanes.

Next implementation order: verify current GET-list batching, add bounded indexed
pagination/ranking; land runner claim renewal; integrate durable queue/identity and
provider reservation/cooldown; gate enrichment/selection; only then measure a single
browser lane and evaluate isolated pooling. This P0 changes none of those packages.
