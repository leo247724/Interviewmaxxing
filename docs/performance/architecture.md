# Interviewmaxxing performance runtime — phased design (P0)

Status: **design + offline benchmark harness**. Nothing in this document changes an MVP
package. Every number is labelled as one of:

| Label | Meaning |
| --- | --- |
| **observed** | Measured on this machine by `benchmarks/performance` (offline, fictional data), or read verbatim from code constants, or quoted from primary provider docs. |
| **modeled** | Computed from observed inputs plus stated assumptions in `benchmarks/performance/imx_perf/config.py`. Change the assumption, the number changes. |
| **external** | Set by the market, a third-party site, or the provider. Not under our control and **not measured** here. |

Base commit for this worktree: `c292f29`. The runtime under discussion is the newer, unmerged
code in the sibling worktrees (their checkpoints are cited inline).

---

## 1. What is being optimized, and what is not

The product thesis (ARCHITECTURE.md §19) is interviews and offers, not applications. This
design therefore optimizes **quality-gated throughput**: how many listings can be found,
enriched, judged, packaged, submitted and reconciled per day **without relaxing a single
existing quality gate**. The gates that stay fixed are listed in §8.

Six volumes are distinct and are reported separately everywhere (docs, benchmark output,
proposed dashboard counters):

| # | Volume | Definition in current code |
| --- | --- | --- |
| V1 | search observations | one `JobStore.upsert` call (J1 `search.py:142`) |
| V2 | unique listings | distinct `JobListing.id` after D0R2 dedupe (`combine`, J1 `store.py:277`) |
| V3 | qualified (eligible) listings | `JobSelection.effective_choice == APPLY` (J2 `service.py`), which is impossible with any hold |
| V4 | packets ready | `ApplicationPacket.is_complete` for the current step (core `packets.py`) |
| V5 | submitted, site-observed | `ApplicationState.SUBMITTED` via an `ACCEPTED` observation (core `store.py:1082`) |
| V5b | submitted, user-reported | `SUBMITTED` via `ReconciliationMethod.USER_CONFIRMED` (S1R receipt `confirmationAuthority: "user"`) |
| V6 | interviews | not modeled by any package yet; pipeline stage text only |

"1000 / 5000 / 10000 quality applications per day" is read as **V5 per day**. §4 shows what
each tier demands upstream and which of those demands are external.

**Plain statement about supply.** For one candidate with one role focus (performance
marketing operator, Austin onsite/hybrid preferred, US remote eligible, ≥ USD 100k), the
number of *new* qualified postings per day is an external quantity that no local
optimization changes and that this task did not measure. The tiers are therefore meaningful
as **platform capacity** (several candidates, several role foci, or listings judged per day),
not as a promise about one person's daily submissions. The harness reports the required
upstream volume for each tier so the gap to observed supply can be read off once a real
search run has been counted (J1's live smoke reports per-source counts).

---

## 2. The runtime as actually built (observed from code)

### 2.1 Discovery (J1, job-ingestion `1d8f2ee`)

* `JobSearchService.run` is synchronous and searches sources **one after another**, each in
  its own OpenCLI session `imx-jobs-<source>` (`search.py:64-96`). Every transport call is a
  `subprocess.run(["opencli", ...])` with a 90 s timeout (`opencli.py:89-103`).
* Per search page: `open` + `wait_for(selector, 15000 ms)` + `eval` (three subprocesses).
  Per detail page: `open` + up to 2 × (`pause(3 s)` + `eval`) on LinkedIn
  (`linkedin.py:304-314`), `open` + `wait_for` + `eval` on Built In / Indeed, a structured
  click + up to 2 × (`pause(1.5 s)` + `eval`) on Google.
* Bounds per run: `max_results_per_source` default 50 (1–500), `detail_limit` 10 per source,
  `max_pages_per_leg` 3, `MAX_BATCHES_PER_TARGET` 8 (`sources/base.py:62`). Austin legs run
  first and get a 4:1 budget share (`sources/base.py:124-128`).
* With the eight default seeds the plan is: LinkedIn 4 legs (two OR-batches × two targets),
  Built In 16, Indeed 16, Google 6. **Modeled** page count for one full run: ≈ 50 search pages
  and 40 detail pages. See `funnel` output for the timing assumptions.
* **Only `detail_limit` listings per source (40 per run) get a full description.** The rest
  are stored from cards: `description_completeness = NONE` (LinkedIn/Indeed/Built In cards)
  or `PARTIAL` (Google). LinkedIn in a background window renders roughly 7 of 25 cards and
  never renders a description (`linkedin.py:8-12`, README).
* `JobStore.upsert` is one `BEGIN IMMEDIATE` transaction per observation behind an `RLock`
  (`store.py:110-168`).
* S2 (`queue-runtime` working tree) drives this with **one search at a time** on a
  single-thread pool and calls `search_source` once per source (`jobs_api.py:359,436-466`,
  `integration.py:202-209`). A different query while one runs is refused with 409.

### 2.2 Selection (J2, jev-selection `0f58421`)

* `SelectionService.select` order (`service.py:177-284`): code checks → hard constraints
  (SKIP, **no Jev call**) → cache (`find_cached(listing_id, cache_key)`,
  `storage.py:149-157`) → **two sequential Jev calls** (six focused questions, then one
  final question with the assessments copied into `state`) → holds → save with full
  snapshots and raw request/response JSON.
* `evidence_holds` (missing/partial description, unknown location, missing profile,
  suspected injection) are computed **before** the Jev calls but do **not** skip them
  (`service.py:199-205`, `policy.py:296-334`). A card-only listing therefore costs two Jev
  calls and can only end in REVIEW.
* Effective APPLY requires all of: verified candidate evidence, `FULL` description
  ≤ 12 000 chars (`evidence.py:31`), known arrangement and location, Jev APPLY with
  confidence ≥ 0.8 (`rubric.py:33-34`), `role_match = match`, and no negative focused answer.
* Cache key = model + rubric version + `job_evidence_hash` + `candidate_evidence_hash` +
  `preferences.fingerprint` (`service.py:143-158`). `job_evidence` includes
  `application_url` and `status` (`evidence.py:61-72`), so enriching a listing with its
  apply link changes the hash and invalidates a cached decision even though Jev never sees
  the URL. **Any** preference edit (including `notes`) invalidates every decision.
* Client: urllib, 20 s timeout, ≤ 3 attempts, backoff 0.5 s / 2 s, a 429 retried only when
  `Retry-After` ≤ 10 s (`jev.py:279-287, 386-396`). No concurrency control exists in the
  client; S2 runs decisions on a **single-thread** pool, at most 10 queued, and only on an
  explicit user click (`jobs_api.py:82-84, 598-626`). There is no bulk auto-decide path.
* `SelectionStore` is one SQLite connection without a lock; S2 opens one per call.
* Provider facts from primary docs (fetched 2026-09-22):
  * OpenRouter model page (`openrouter.ai/typesafe/jev-1.13/api`): "$0.042 / $0 per 1M"
    input/output, 32K context. No batch endpoint, no rate or concurrency limits documented.
  * TypeSafe Choice docs: "Ask every Choice question your code might need in a single
    request rather than one request per question. Questions are evaluated in parallel."
    Up to 255 options per question.
  * TypeSafe confidence docs: "Start with conservative thresholds, test with your own data,
    and adjust as you observe results."
  * OpenRouter limits page: no requests-per-second or concurrency figures for paid models;
    Cloudflare protection "will block requests that dramatically exceed reasonable usage";
    nothing specific to `/api/alpha/decisions`; **no batch API**.
  * OpenRouter Jev tutorial: triage benchmark p50 0.194 s, p95 0.633 s; "~$0.000025" per
    decision; 64k tokens per request, 32k for state plus the longest question; Jev "tends to
    lose accuracy when the state it examines carries irrelevant detail"; "Ask one small
    judgment per question and combine the answers in code."
  * Consequence: **batching across listings is not proposed** (no such API). Multi-question
    requests are used already and are the documented pattern.

### 2.3 Submission (I1 core-contracts `d43ce6b`, C4/O1 browser-ats `983192c`)

* `LocalApplicationRunner` takes an OS `flock` on the persistent browser profile
  (`runner.py:132-148`) and a store claim per application (`runner.py:329-349`). Every run
  launches a fresh Chromium persistent context and closes it at the end
  (`session.py:76-110`); Chromium allows one process per user-data-dir. **Concurrency is
  exactly one application per profile.**
* S2's `Dispatcher` runs one executor call at a time on one event-loop thread and refuses a
  second run with `ExecutorBusy` (HTTP 409) rather than queueing (`executor.py:96-143`).
* Run limits: 12 pages, same form ≤ 3 times, 600 s wait for user action, 300 s claim TTL
  (`runner.py:110-117`). Browser: 10 s per action, 15 s settle (`session.py:65-74`).
* Store guarantees relied on (all in `store.py`): `UNIQUE (candidate_id, job_id)` (line 126);
  each operation is one `BEGIN IMMEDIATE` transaction with WAL and `synchronous = FULL`
  (283); `claim` with token + expiry and automatic `SUBMISSION_UNKNOWN` on a lapsed
  `SUBMITTING` owner (768-794, 816-838); `begin_submission` before the click with a ≥ 10 min
  lease (1000-1046); `SUBMITTED` only from an `ACCEPTED` observation or reconciliation.
  These are the authority and are **not** redesigned.
* OpenCLI path (O1): one owned tab in the user's Chrome, commands serialized by an
  `asyncio.Lock` (`opencli.py:217`), file upload only where Browser Bridge may set files
  (372-392), multi-select limited to one option (348-353), no window focus. It is a
  user-present path by construction; its throughput is bounded by the user.
* Reconciliation (`runner.reconcile`) needs the same browser profile lock, so it competes
  with applications for the single browser.

### 2.4 Human input

* `EXPLICIT_ANSWER_REQUIRED` types (work authorization, sponsorship, salary, consent,
  attestation, EEO, pronouns) are answerable only from a `SavedAnswer` or a `UserInput`
  (CONTRACTS.md §4). A question is identified by (form scope, field id, fingerprint);
  wording changes create a new question. The first application on any ATS tenant whose
  consent/attestation wording is new therefore stops in `NEEDS_INPUT` at least once.
* S2 never asks inline: the run records `NEEDS_INPUT` and the frontend asks
  (`executor.py:49-68`). Reuse scope defaults to the application; `GLOBAL` reuse is the
  user's explicit choice.

### 2.5 Local store costs (observed by `measure`, see `docs/performance/benchmarks.md`)

The harness measures, on a temporary database, the cost of the canonical operations a run
performs (`record_request`, `claim`, `transition`, `save_packet`, `begin_submission`,
`record_submission_outcome`, `release`), `JobStore.upsert` throughput with fictional
listings, `SelectionService.select` on the hard-constraint and cache-hit paths, and the full
two-call path with a fake transport. Those are local CPU/fsync costs only.

---

## 3. Stage model

Seven stages, each with its own queue lane, resource pool and counters. Stage names are
used verbatim by the harness and by the proposed queue schema.

| Stage | Input → output | Resource | Current concurrency | Proposed bound |
| --- | --- | --- | --- | --- |
| `discover` | query → V1 observations | user's Chrome via OpenCLI, one session per source | 1 search, sources sequential | 1 per source session; ≤ `chrome_cmd_parallel` commands per Chrome profile (default 2, **to verify** against OpenCLI) |
| `enrich` | card-only listing → detail page (`FULL` description, apply link, `employer_job_key`) | same Chrome sessions, or an ATS posting page | folded into `discover` (`detail_limit`) | separate lane, ordered by cheap gates, budgeted per source per hour |
| `semantic_fit` | listing + prefs + candidate → `JobSelection` | Jev Decisions API | 1 thread, user click only | `jev_inflight` (default 4, adaptive on 429), daily USD budget, priority by tier |
| `candidate_packet` | form inspection + candidate → `ApplicationPacket` | CPU + candidate store | inside the run | inside the run (unchanged) |
| `submission` | APPLY listing → `SUBMITTED` / `NEEDS_INPUT` / `SUBMISSION_UNKNOWN` | Playwright profile(s) | 1 | `browser_slots` profiles (default 2 on a laptop), 1 per ATS tenant, per-tenant minimum spacing |
| `reconciliation` | `SUBMISSION_UNKNOWN` → `SUBMITTED` / `FAILED_RETRYABLE` / unchanged | a browser slot (lower priority) | shares the single browser | shares `browser_slots`, priority below first submissions, retried on a schedule |
| `human_input` | `NEEDS_INPUT` question set → `UserInput`s | the user | frontend polling | a distinct-question queue ranked by blocked applications; answer-once with `GLOBAL` reuse offered |

---

## 4. Targets, budgets and where the ceilings are

Assumptions (all editable in `imx_perf/config.py`; defaults are conservative guesses,
**modeled** unless marked):

| Symbol | Default | Basis |
| --- | --- | --- |
| `p_dedupe` | 0.35 | share of observations that are re-observations of a known listing (guess; measure from `JobStore.observations`) |
| `p_cheap_pass` | 0.55 | share of unique listings surviving code gates (closed, pay below floor, onsite mismatch, excluded terms) (guess) |
| `p_enrich_needed` | 0.80 | share of cheap-pass listings without a `FULL` description (observed structure: 160 of 200 per run are card-only) |
| `p_apply_given_enriched` | 0.25 | share of enriched, gated listings Jev marks effective APPLY (guess; needs the labeled fixture set) |
| `p_needs_input` | 0.30 | share of first submissions per tenant that stop in `NEEDS_INPUT` at least once (guess) |
| `p_unknown` | 0.05 | share of submits ending `SUBMISSION_UNKNOWN` (guess) |
| `t_search_page_s` | 6 | open + wait + eval through three `opencli` subprocesses (modeled from timeouts; unmeasured) |
| `t_detail_page_s` | 5 | one detail page (modeled) |
| `t_jev_call_s` | 0.5 | per call; vendor triage p50 0.194 s, J2 README 0.5–2 s per two-call select (observed range, external) |
| `jev_input_tokens_per_call` | measured from serialized request size ÷ 4 | estimate; provider `usage` is the truth |
| `t_submit_s` | 90 | one single-page application end to end incl. Chromium launch, no user input (modeled, unmeasured) |
| `t_reconcile_s` | 45 | one site recheck (modeled) |

With those defaults the harness (`python -m imx_perf funnel`) prints, per tier, the daily
volumes each stage must sustain and the average concurrency by Little's law
(`concurrency = rate × service time`). Indicative results (rerun to regenerate; the
authoritative table is in `docs/performance/benchmarks.md`):

| Tier (V5/day) | enriched listings/day | Jev calls/day (2-call) | browser-hours/day for submission | avg. browser sessions |
| --- | --- | --- | --- | --- |
| 1 000 | ≈ 4 000 | ≈ 8 000 | ≈ 25 h | ≈ 1.1 |
| 5 000 | ≈ 20 000 | ≈ 40 000 | ≈ 125 h | ≈ 5.2 |
| 10 000 | ≈ 40 000 | ≈ 80 000 | ≈ 250 h | ≈ 10.4 |

Reading the table honestly:

1. **Jev is not the bottleneck.** 80 000 calls/day at ≈ 4–5k input tokens is on the order of
   USD 15–20/day at the documented price, and vendor latency is sub-second. Provider
   concurrency limits are undocumented, so the design keeps an adaptive in-flight cap and
   treats 429s as a signal, never as something to route around.
2. **Enrichment page loads are the first real ceiling.** 40 000 detail pages/day at 5 s each
   is 55 h of page loads per day: impossible in one Chrome session and, more importantly, far
   beyond what any of the four sources would serve without challenges. The design never
   bypasses a challenge (§8), so effective discovery capacity is **external**.
3. **Submission browser-hours are the second ceiling.** 10 sessions around the clock means
   10 Chromium profiles, 10 sign-in states, and 10 windows a human may need to act in.
4. **Human input caps unattended throughput.** With `p_needs_input = 0.30`, tier 1 000 means
   ~300 applications/day waiting on the user at least once. Answer-once reuse reduces this
   after the first application per tenant, but the first one always waits.
5. **Market supply is external** and, for one candidate and one role focus, is almost
   certainly far below tier 1 000 per day.

The harness therefore reports tiers as **capacity requirements**, not as achievable
throughput, until a real run supplies V1/V2 counts.

---

## 5. Phased plan

Each phase is additive, lands behind the existing packages' public APIs, and can be
verified offline with the harness plus the packages' own fixture tests.

### Phase 0 — Measure and fix the vocabulary (this task)

* This document, the six volumes, the stage model, the assumptions file, the harness.
* Offline microbenchmarks of the canonical store, the job store and the selection service.
* A durable-queue prototype with a chaos test proving lease/idempotency/resume behavior
  against the same invariants the core store enforces.

### Phase 1 — Durable work queue around the canonical stores (queue-runtime, WT-07 scope)

A scheduler, **not** a second state machine. Work items reference canonical records; the
canonical stores keep every guarantee.

Schema (prototype in `benchmarks/performance/imx_perf/durable_queue.py`):

```
work_items(id, kind, key UNIQUE-while-active, lane, priority, payload, state,
           attempts, max_attempts, not_before, lease_owner, lease_token, lease_expires_at,
           created_at, updated_at, last_error)
lane_limits(lane, max_active)                  -- browser_profile:<dir>=1, ats_tenant:<host>=1,
                                               -- source:<slug>=1, jev=<n>, chrome:<profile>=<n>
events(seq, item_id, event, at, metadata)      -- append-only, like the core store's events
```

Semantics, matching the core store's `Claim`:

* `enqueue(kind, key, ...)` is idempotent: one active item per `(kind, key)` (partial unique
  index, as S2's `tasks_one_active`). Keys: `apply:<application_id>`,
  `reconcile:<application_id>`, `decide:<listing_id>:<prefs_fingerprint>`,
  `enrich:<listing_id>:<source>`, `search:<query_key>`.
* `claim_next(lanes, owner, ttl)` takes the highest-priority READY item whose lanes all have
  a free slot, in one `BEGIN IMMEDIATE` transaction, and returns a lease (owner, token,
  expiry). `renew`, `complete`, `fail(retry_in|permanent)`, `release` all check the token.
* `recover_expired()` returns lapsed leases to READY with `attempts + 1`; after
  `max_attempts` the item is DEAD_LETTER with its last error. This is the queue-level
  resume. **Application-level resume stays the runner's**: a re-claimed `apply` item calls
  `runner.resume(application_id)`, whose store claim refuses while the old claim is live
  (`ClaimUnavailable`, treated as a transient), and whose store converts a lapsed
  `SUBMITTING` into `SUBMISSION_UNKNOWN` before any browser is opened. A `SUBMISSION_UNKNOWN`
  outcome moves the item to the `reconcile` lane; `SUBMITTED`, `DUPLICATE`,
  `FAILED_PERMANENT`, `WITHDRAWN` complete it.
* Backpressure: each lane has a high-water mark; `discover` stops paging a source when
  `enrich` or `semantic_fit` depth is above it, and `enrich` stops when `submission` READY
  depth is above its mark. Ordering within a lane is by `(priority, created_at)`, where
  priority is location tier, then Jev APPLY probability, then recency, so a bounded queue
  never starves Austin roles.
* Budgets: per-lane daily counters (`jev_usd`, `page_loads:<source>`, `questions_asked`)
  refuse new claims when exhausted and emit an event; nothing silently drops.
* Runner integration: the queue worker for `apply` items constructs the runner exactly as
  S2 does (`integration.py:146-166`), one runner per run on the worker's thread, with a
  `BrowserOptions.profile_dir` chosen from the `browser_profile:*` slot it holds. Nothing
  else about the runner changes.

Verification: the chaos test (`python -m imx_perf chaos`) kills random workers mid-item and
proves every item completes exactly once, no simulated submit effect is applied twice, and
all leases recover. This is WT-07's acceptance criterion ("kill workers randomly during a
100-job test") run against the prototype.

### Phase 2 — Cheap, fast Jev at scale (jev-selection + queue-runtime)

Ordered by how much they save per the harness (`python -m imx_perf selection`):

1. **Gate before Jev, not after.** If a pre-Jev hold makes effective APPLY impossible, do not
   spend the calls:
   * curable holds (`MISSING_LISTING_DETAILS`, `INCOMPLETE_DESCRIPTION`, `LOCATION_UNKNOWN`)
     → route to `enrich` first, decide after enrichment;
   * incurable holds (`MISSING_PROFILE`, `SUSPECTED_INSTRUCTION_INJECTION`) → REVIEW at once
     with the hold recorded, no call.
   Contract effect: none (the `JobSelection` shape already expresses a hold without a
   model decision). Behavior change inside `SelectionService.select`; needs J2 owner review.
2. **Compact final call.** The second call resends the full state including the description.
   The final question is asked to weigh the assessments and code checks; the description is
   the largest, least relevant field for that question, and the vendor documents that
   irrelevant state hurts accuracy. Proposal: a rubric-versioned variant whose final `state`
   carries the listing header (title, company, location, arrangement, pay text) but not the
   description. **Accuracy must be checked on the labeled fixture set before default use**;
   the harness only quantifies tokens, cost and latency.
3. **One request, seven questions (A/B candidate).** The documented pattern. The final answer
   cannot see the focused answers, so the combination logic moves entirely into code
   (`assessment_holds` already does most of it). Same rule: labeled eval first.
4. **Cache and invalidation hygiene.**
   * Exclude `application_url` from `job_evidence_hash` (Jev never sees it) so enrichment of
     the apply link does not invalidate a decision. Additive change request to J2.
   * On a preference edit, re-run code checks immediately for every listing (microseconds),
     and re-decide with Jev in priority order under the daily budget: previously APPLY and
     REVIEW first, PREFERRED tier first, then SKIPs. Stale decisions stay visible as stale
     (S2 already renders `stale`).
   * Never cache provider failures (already true).
5. **Bounded concurrency and retries.** A `jev` lane with `max_active` in-flight calls
   (default 4), decreased on any 429 and slowly increased back; `Retry-After` honored as
   today; per-day USD budget from the provider's returned `usage.cost`; a circuit breaker
   that holds the lane for 401/402/403 (not retryable) and surfaces the action hint.
6. **Selective one-vs-two-call decision rule (default until evals exist):** two calls for
   everything that reaches Jev, with (1) applied. Switch to (2) or (3) only per rubric
   version after the labeled comparison, keeping `returned_model` and `rubric_version` on
   every record so old and new decisions are never mixed in the cache.

### Phase 3 — Enrichment as its own stage (job-ingestion)

* Split `detail_limit` out of the search: search stores cards quickly; an `enrich` worker
  opens detail pages **only for listings that pass code gates and are not already FULL**, in
  tier order, under a per-source hourly page budget. This turns 40 enriched listings per
  run into "as many as the budget and the site allow".
* Prefer the employer's ATS posting page (Greenhouse, Lever, Ashby, SmartRecruiters,
  Workday, recognized by `employer_key_from_url`) as the enrichment source when a
  job-specific apply link is known: it yields the full description, a proven
  `employer_job_key` for cross-source dedupe, and the exact application URL in one page load.
* Keep one OpenCLI session per source; allow the four sources to run concurrently only after
  verifying with OpenCLI's documentation that concurrent commands on different owned sessions
  of one Chrome profile are supported (unverified here). Until then, interleave sources on
  one worker and let `enrich` run between searches.
* Re-observation policy: a listing seen again within `reobserve_after_h` (default 24 h) is
  not re-enriched; `CLOSED` stays closed (existing `combine` rule).

### Phase 4 — Submission slots and pacing (core-contracts + browser-ats)

* `browser_slots` persistent profiles (`$IMX_BROWSER_DIR/slot-<n>`), each with its own
  `flock`, each requiring its own sign-ins. The runner is unchanged; the queue worker passes
  the slot's `profile_dir`. Default 2 slots on a laptop; memory per Chromium context is
  **unmeasured** and must be measured before raising it.
* Per-ATS-tenant lane (`ats_tenant:<host>`, `max_active = 1`, minimum spacing between
  submissions to the same tenant, default 120 s). Per-domain pacing is a quality and
  courtesy control, not a stealth measure; a CAPTCHA or sign-in page always parks the
  application in `NEEDS_INPUT` for the user (§8).
* Keep a warm Chromium per slot between runs (launch once per slot, `open` per
  application) to remove the per-run launch cost. This is a change to
  `PlaywrightSessionFactory` usage, not to the `ApplicationBrowser` contract, and must keep
  "one run per profile at a time".
* Reconciliation items use the same slots at lower priority, with exponential spacing
  (15 min, 1 h, 6 h, daily) and never resubmit.

### Phase 5 — Human input as a first-class queue (dashboard + queue-runtime)

* A `questions` view that groups `NEEDS_INPUT` items by normalized question text and
  semantic type, ordered by the number of applications each unanswered question blocks.
* Answering once with `reuse = GLOBAL` (user's choice, unchanged contract) releases every
  blocked application whose question fingerprint matches; `JOB` reuse releases one.
* The harness's `pipeline` simulation reports how many distinct questions per 100
  applications appear under the fictional question catalog and how the blocked count decays
  with answer-once reuse.

### Phase 6 — Outcome counters (later)

V6 and the application → screen → interview rates from ARCHITECTURE.md §18. Out of scope
here beyond reserving the counters.

---

## 6. Latency and concurrency budgets (modeled)

| Stage | Per-item budget | Concurrency default | Notes |
| --- | --- | --- | --- |
| discover (search page) | 6 s (p95 20 s; hard 15 s wait + 90 s subprocess timeout) | 1 per source session | external site latency |
| enrich (detail page) | 5 s (p95 15 s) | 1 per source session; page budget/hour | external |
| semantic_fit (2 calls) | 1 s p50, 3 s p95 (vendor p95 0.63 s per call + network) | `jev_inflight` 4 | USD budget/day |
| candidate_packet | < 50 ms | inside run | measured `resolve` is CPU-bound |
| submission (single page, no input) | 90 s (p95 240 s) | `browser_slots` 2, 1 per tenant | unmeasured; includes Chromium launch today |
| reconciliation | 45 s | shares slots, lower priority | never resubmits |
| human_input | minutes to days | the user | answer-once reuse |
| store ops per application | ≈ 15 fsync'd transactions | n/a | measured in `benchmarks.md` |

---

## 7. Seams and risks for review

1. **Card-only listings burn Jev calls and can never be APPLY** (§2.2). Cheapest large win;
   needs a J2 behavior change (gate before call) and the `enrich` lane.
2. **`application_url` in the evidence hash** invalidates decisions on enrichment. Additive
   J2 change; quantified by the harness.
3. **Single browser profile = concurrency 1** for submission and reconciliation combined.
   Multiple slots multiply sign-in effort and memory; both are user-visible costs.
4. **Preference edits invalidate everything.** Fine at 200 listings; at 20 000 it is a
   re-decide storm unless prioritized and budgeted.
5. **OpenCLI subprocess-per-command** through the user's Chrome is the discovery transport.
   Concurrency across sessions is unverified; background tabs do not render LinkedIn
   descriptions. Enrichment through employer ATS pages sidesteps the LinkedIn rendering
   limit but not site rate limits.
6. **S2 refuses rather than queues** (`ExecutorBusy`, 409). The queue in Phase 1 replaces that
   with durable admission; the service keeps its "one run per profile" rule.
7. **Human input is the unattended-throughput cap.** No design removes it; answer-once reuse
   is the only lever consistent with the never-infer rule.
8. **Nothing here measures live employer throughput.** All browser timings are modeled.

---

## 8. Quality gates that stay fixed

* Actual verified qualifications and critical job evidence only: `CandidateEvidence` from
  verified facts; `FULL` description required for APPLY; holds as today.
* Semantic role match (`role_match` judged on duties), never an exact-title filter; only the
  user's explicit `excluded_keywords` drop titles.
* Missing answers hold: `NEEDS_INPUT`, never inferred; `EXPLICIT_ANSWER_REQUIRED` only from
  saved answers or user input.
* Source-job dedupe by proven keys only (`posting_key`, `employer_job_key`); titles,
  companies, search pages and generic apply endpoints never merge.
* No fabricated facts or claims: packets carry provenance; `provenance_problems` enforced.
* Observed acceptance (`ACCEPTED` signals tied to the job) is separate from user-reported
  confirmation (`USER_CONFIRMED`); the receipt says which.
* `SUBMISSION_UNKNOWN` is never retried automatically.
* No CAPTCHA/sign-in/anti-bot bypass; a user-action page always waits for the user. Per-site
  pacing is a courtesy limit, not evasion.
* Jev never does arithmetic, dates or duplicate detection; code does.
* One application per (candidate, canonical job); one open submission attempt.

---

## 9. Contract change requests (additive, for the coordinator)

| # | Package | Change | Why | Evidence |
| --- | --- | --- | --- | --- |
| 1 | J2 | `SelectionService.select(..., enrich_first=True)`: skip Jev when a pre-call hold makes APPLY impossible; return a `JobSelection` with the hold and no model decision, plus an `enrichment_needed` flag on `SelectionOutcome` | saves the calls for card-only listings; produces the same effective REVIEW | `selection` benchmark: share of calls avoided |
| 2 | J2 | drop `application_url` from `job_evidence` (keep it in the audit snapshot) | apply-link enrichment must not invalidate a decision | `selection` benchmark: invalidations avoided |
| 3 | J2 | rubric-versioned compact final-call state (no description) and a single-request 7-question variant, both behind flags, plus a labeled fixture eval command | cost/latency; accuracy unknown until measured | `selection` benchmark: tokens/cost/latency per strategy |
| 4 | J1 | `JobSearchService.run(..., detail_limit=0)` + `enrich(listing_ids)` entry point on the same adapters | separate enrichment lane | Phase 3 |
| 5 | core | `BrowserOptions.profile_dir` already suffices for slots; request only a documented convention `$IMX_BROWSER_DIR/slot-<n>` and a `RunLimits` field for per-tenant spacing | multiple browser slots | Phase 4 |
| 6 | S2 | replace `ExecutorBusy` refusal with enqueue on the durable queue for `apply`/`resume`/`reconcile`; keep 409 only for a request that would violate a store disposition | durable admission | Phase 1 |
