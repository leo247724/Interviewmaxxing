# Timed find-jobs runbook: Austin 100

This run finished with **100 distinct new Saved leads**, verified in both the local API and the dashboard. The timer ran for **44 minutes, 14 seconds** (2,654.753 seconds), including discovery, review, duplicate checks, corrections, imports, and final verification. This historical record supplied the evidence for the reusable skills below. It does not establish that the task-local collector changes shipped or that the production application can repeat this run unattended.

## Reusable Codex and Claude skills

Use [find-jobs](../skills/find-jobs/SKILL.md) for the complete workflow, [find-jobs-source](../skills/find-jobs-source/SKILL.md) for each source worker, and [review-job-candidates](../skills/review-job-candidates/SKILL.md) for semantic and evidence review. These skills replace the historical one-off commands below for future runs. They keep constraints configurable, use a strict reviewed-artifact contract, check duplicates before details, overlap source collection with batch review, and count only the current run's verified Saved cards.

From this checkout, preview or install the shared bundle:

```bash
python3 skills/find-jobs/scripts/install.py
python3 skills/find-jobs/scripts/install.py --install
```

The installer maintains copies under `~/.agents/skills`, with individual links under the selected Codex home and `~/.claude/skills`. It does not depend on this worktree remaining present. It preflights every bundled name and refuses to overwrite unmanaged skills or local edits. After editing repository sources, rerun it to update the managed copies. An alternate `--home` supports isolated installation tests. The later [find-jobs-fast-batches](../skills/find-jobs-fast-batches/SKILL.md) adds compact tool output, shared browser queueing, and candidate validation for larger worker fleets.

The seven-skill bundle also includes [coordinate-worker-pool](../skills/coordinate-worker-pool/SKILL.md) for bounded assignments and stage balancing, [worker-resource-budget](../skills/worker-resource-budget/SKILL.md) for read-only host/process measurements, and [find-jobs-save-batches](../skills/find-jobs-save-batches/SKILL.md) for the sole pipeline writer. Load only the skill needed for the current role. The [measured lessons](../skills/coordinate-worker-pool/references/lessons.md) distinguish observed bottlenecks from improvements that still need a comparable full-run benchmark.

Invoke `$find-jobs` in Codex or `/find-jobs` in Claude Code with the requested target and scope. Codex supports personal skills in `~/.agents/skills` and detects changes automatically; restart if an update does not appear. Claude Code supports personal skills and individual symlinked folders. See the official [Codex skill documentation](https://learn.chatgpt.com/docs/build-skills) and [Claude Code skill documentation](https://code.claude.com/docs/en/skills). Neither invocation by itself configures a missing Interviewmaxxing checkout, working environment, browser sign-in, or local service.

## Historical run receipt

The timer started at **2026-09-23T02:20:05.832936Z** and stopped at **2026-09-23T03:04:20.586160Z** (September 22, 9:20–10:04 p.m. Central). The start was preserved through retries and source changes. Scope: Austin, Texas, onsite or hybrid digital/performance marketing, with LinkedIn, Google Jobs, and Built In searched concurrently. Employer sites and other public marketplaces supplied additional verification and leads. The USD100,000 annual compensation requirement remains an application gate: unknown pay and ranges crossing the floor were saved with review flags. This run authorized **saving jobs, not applications or employer messages**. The original eight tracker entries remained exactly unchanged.

The final [receipt](../.imx/austin-100/final-receipt.json), [100 listing records](../.imx/austin-100/final-records.json), [canonical Saved receipts](../.imx/austin-100/saved-receipt-canonical.json), and [dashboard snapshot](../.imx/austin-100/ui-final-snapshot.txt) establish the result. Raw evidence remains private under ignored `.imx/austin-100/`; it is local evidence, not a portable checked-in dataset. The 23 generated browser artifacts were moved from `.playwright-cli/` into the private task archive after closing owned sessions; the [path mapping](../.imx/austin-100/playwright-artifact-map.json) resolves older snapshot references. Never copy the private [baseline snapshot](../.imx/austin-100/pipeline-before.json) into a shareable report.

| Final check | Result |
| --- | --- |
| New Saved cards | 100; 100 distinct canonical listing IDs; no repeated observed employer ATS keys |
| Austin work arrangement | 71 hybrid; 29 onsite, with source conflicts or cadence caveats retained in notes |
| Published comparable pay | 28 ranges start at or above USD100,000; 8 start below it but reach the floor |
| Pay requiring confirmation | 64 unknown, estimated, or otherwise noncomparable; 72 require pay review including the 8 crossing ranges |
| Employer availability | 73 OPEN observations; 27 UNKNOWN and require employer verification before applying |
| Canonical description coverage | 34 FULL; 66 partial or paraphrased, with requirements-review notes |
| Existing tracker | Original 8 unchanged; 10 screened-out additions remain in Closed with exclusion notes; 118 total cards |
| Application boundary | Zero applications and zero submission attempts; service remained TEST_ONLY |

These are **Saved research leads**, not 100 salary-confirmed, application-ready vacancies. Recruiter client identities, employer availability, qualifications, and compensation still need the reviews recorded on each card. LinkedIn produced 122 observations; Google Jobs 109; Built In 421 cards and 123 detail inspections. These counts overlap and include exclusions. Canonical source attribution differs from discovery attribution: the saved records retain 40 LinkedIn, 26 Built In, 4 Google, and 30 employer or supplemental-source identities.

## Verified workflow

1. **Freeze scope, timer, and baseline.** Keep the target and original start in `run.json`. Retain the baseline snapshot for equality checks, without exposing existing personal tracker details. Confirm the intended local service and canonical job store before any import.
2. **Acquire into staging.** Each source owns its own browser session and evidence files. Keep source IDs, posting URLs, observation timestamps, raw cards, detail evidence, arrangement/location evidence, salary wording, and errors. A card can justify further inspection; it is not semantic approval.
3. **Retrieve and read the job description.** Verify that the displayed detail belongs to the selected card. Resolve conflicting card, structured-data, and description claims before counting. Record partial descriptions honestly and require further review; do not relabel a summary or teaser as a full description.
4. **Approve semantically into explicit approved files.** Review actual responsibilities, Austin attendance, availability, compensation, and duplicate risk. Write a concise fit rationale and review reasons. Records outside the approved set remain excluded or pending. Relevant title seeds are search aids, not an eligibility allowlist.
5. **Preview the approved import.** Pass only named `*-approved.json` files to `save_verified.py`. Inspect duplicate holds and proposed actions. Do not import provisional `linkedin.json`, generic collector output, or a wildcard covering all JSON files.
6. **Upsert canonical listings, then track.** The importer validates `JobListing`, calls canonical `JobStore.upsert`, uses the returned canonical ID in local `POST /jobs/{id}/track`, and obtains the pipeline entry ID. This handles aliases after a proven duplicate merge. Tracking saves a card; it does not apply.
7. **Write review notes using the current revision.** Read the newly tracked entry, preserve its other fields, and update location/arrangement, source pay bounds, fit rationale, uncertainty, and next review action. The importer uses the entry's fresh revision for the local pipeline update.
8. **Verify the resulting Saved state.** Re-read the pipeline, confirm the original eight entries are unchanged, count only new cards in the Saved lane, and reconcile receipt IDs with canonical listing/card IDs. Continue until the verified count reaches 100. Do not count closed cards, duplicates held for review, unsaved approved rows, or source observations.

Implementation evidence: [task importer](../.imx/austin-100/save_verified.py), [canonical JobStore](../packages/jobs/src/interviewmaxxing_jobs/store.py), [tracking route implementation](../apps/service/src/interviewmaxxing_service/jobs_api.py), and [revision-aware pipeline updates](../apps/service/src/interviewmaxxing_service/pipeline_api.py).

Saved cards feed mass preparation through the application URL inventory, whose rows carry each job's `listing_id` and Saved card `pipeline_id`. `interviewmaxxing prepare-batch` links each resulting application to its Saved card; it creates no cards and never moves a card to Applied. When it observes that a job is closed, it moves the job's Saved card to Closed with a dated history note through the revision-aware pipeline move, which quotes the card's fresh revision as in step 7. `interviewmaxxing batch-report` then groups the remaining holds by category. Preparation never submits. See [mass-preparation.md](mass-preparation.md).

## Browser and source isolation

Use owned sessions named `imx-jobs-<source>`; this run uses LinkedIn, Google, and Built In sessions separately. Never bind, navigate, close, or repurpose an unrelated user tab. One worker owns a source session at a time; parallel work must use separate sessions and staging files. Refresh browser state after navigation or a DOM-changing interaction before reusing element references.

The J1 [OpenCLI transport](../packages/jobs/src/interviewmaxxing_jobs/opencli.py) validates the session-name prefix and issues structured argument arrays. Extraction scripts read the DOM; navigation and clicks use structured browser commands. Authentication walls and challenges are observable blockers, not permission to replay private APIs or bypass authentication. Preserve a `NEEDS_USER` session for the user's action; record partial progress instead of calling an empty or blocked search successful. The installed LinkedIn search command's private Voyager route is explicitly excluded by the [J1 README](../packages/jobs/README.md).

Task collectors contain run-specific profile/tab IDs and staging paths. Those IDs are not reusable defaults. Re-establish ownership and live state before reusing the workflow; do not blindly rerun a script against an old tab ID. Foregrounding is permitted only for an owned job-search tab.

## Source findings and recovery patterns

| Source/problem | Verified pattern | Operational consequence |
| --- | --- | --- |
| LinkedIn background rendering | Background pages reported `document.hidden`; only some cards rendered and full descriptions remained absent despite waits. Moving the owned job-search session to foreground allowed full descriptions to render. | Inspect visibility and description completeness before increasing wait time or accepting card-only evidence. Foreground the owned tab, revisit job-specific pages, and persist the improved observations. |
| Google result selection | Clicking the result container could leave a different detail pane active. | Open that result's own observed `share_url`, refresh state, and check pane identity against the selected title/company before extracting links or description. |
| Google collapsed descriptions | A correctly matched pane could still contain “Show full description.” | Find the unique expansion control in the active pane, use its fresh structured reference, click, wait, refresh state, and recheck both identity and expansion. The task workaround waited one second and, if still collapsed, another second. These observed timings are not a universal readiness guarantee. |
| Built In result freshness | An earlier collection checkpoint contained 318 unique cards across 13 search pages, but many detail records had expired `validThrough` values. Acquisition has continued beyond that checkpoint; these are not final counts. | Search presence is not proof of an active vacancy. Inspect expiry, closure text, and current employer availability; exclude expired records unless independently reverified. |
| Built In location contradiction | A Danaher Austin card described fully onsite work in Washington, DC or Boston. | Exclude it from this Austin run; do not overwrite contradictory description evidence with the card's city or schema location. |
| Built In arrangement contradiction | A Portside in-office card described fully remote work. | Exclude it from this onsite/hybrid run. Mixed “in-office or remote” labels also need explicit Austin attendance evidence. |
| Marketplace versus primary closure | Four Seasons appeared in current search results while the employer marked the vacancy filled. UFCU's exact employer page showed its Digital Growth Manager role closed July 22. | A fresh-looking search date does not reopen a vacancy. Preserve the closure evidence and exclude it. |
| Expired copy versus active replacement | ShipperHQ's old JazzHR URL returned 410; its current employer careers board linked a different live posting. Prophet, Sonar, FloSports, and other roles were recovered through current primary pages. | Verify the current employer job identity. Neither a dead syndicated copy nor a surviving cached description settles current availability. |
| Aggregator title and pay rewriting | Google surfaced an embellished Elevate title and a 70–100K range; the current employer posting says Marketing Manager (Horse Racing League) and only “competitive salary.” Optimal's Google 145K figure was not employer-published. | Keep original source observations, but use the employer title and leave unverified pay unknown. Never silently convert an aggregate estimate into employer compensation. |
| Cross-source employer aliases | Cintra/Ferrovial, SoGal/Everlywell, Visa/Visa U.S.A., and recruiter reposts produced duplicate candidates. Zello's renamed Demand Generation listing retained a growth-marketing URL overlapping an old tracker process. | Use requisition and employer evidence; hold uncertain reposts instead of incrementing the target. Company/title normalization alone misses aliases. |

LinkedIn evidence: [initial background collector](../.imx/austin-100/linkedin_collect.py), [foreground continuation](../.imx/austin-100/linkedin_resume.py), [current observations](../.imx/austin-100/linkedin.json), and [approved/rejected judgments](../.imx/austin-100/linkedin-approved.json).

Google evidence: [initial conservative curation](../.imx/austin-100/google-curate.py), [share-URL and expansion workaround](../.imx/austin-100/google-expand.py), [expanded raw observations](../.imx/austin-100/google-expanded-raw.json), [manual judgments](../.imx/austin-100/google-judgments.json), and [approval conversion](../.imx/austin-100/google-judge.py).

Built In evidence: [card collection](../.imx/austin-100/builtin_collect.py), [staged observations](../.imx/austin-100/builtin.json), [explicit approval/exclusion decisions](../.imx/austin-100/builtin-decisions.json), [Danaher detail](../.imx/austin-100/builtin-detail-11289910.json), [Portside detail](../.imx/austin-100/builtin-detail-11237629.json), and [approval conversion](../.imx/austin-100/builtin_approve.py).

## Semantic, compensation, and duplicate decisions

**Judge responsibilities, not isolated words.** Evidence of paid search/social, acquisition-channel budgets, CAC/ROAS, conversion optimization, lifecycle acquisition, demand generation, and attributable pipeline can support relevance. A broad marketing title needs supporting responsibilities. “Acquisition” can mean corporate M&A, and product positioning, messaging, launch narratives, recruiting growth, or content production do not by themselves establish digital/performance acquisition ownership. Adjacent analytics, operations, or leadership roles require an explicit rationale and scope review.

An early provisional LinkedIn eligibility decision admitted a product-positioning role that was later closed. Subsequent review also removed analytics, technical transformation, and creative-production roles whose descriptions assigned campaign ownership to other people. The corrective workflow is **reviewed, explicitly selected records only**, followed by independent checks of borderline duties. Even a source's approved file can contain known cross-source duplicates or a role later retracted. Current collector booleans and keyword heuristics are discovery signals. The importer trusts `eligible` flags and does not enforce approved filenames or independently judge full-description relevance; parent file selection and exclusion overrides remain essential controls. See [scope corrections](../.imx/austin-100/scope-corrections.json), [LinkedIn judgments](../.imx/austin-100/linkedin-approved.json), and [importer eligibility handling](../.imx/austin-100/save_verified.py).

The distinction must remain semantic: UL's Senior Product Marketing Specialist was retained because its duties explicitly include paid media, SEO, ABM, and demand execution. Its Industry Marketing Director was held because positioning and leadership dominated the evidence. A paid-search product title can qualify when the description requires hands-on SEM execution. Conversely, a title containing “growth” or “lifecycle” does not qualify when the job only supplies reporting, data governance, assets, or campaign logistics to channel owners. An executive or consulting role needs a separate qualification review even when campaign ownership is real.

**Apply the stated compensation rule precisely.** A comparable published annual USD range qualifies for review if its upper bound reaches USD100,000. If its lower bound is below the floor, retain the actual range and flag that an offer at or above the floor must be confirmed. A published upper bound below the floor excludes the role. Missing salary, unconfirmed currency, estimated pay, and unsupported period conversions remain unknown/review, never fabricated amounts. A lower-bound-only figure below the floor does not prove that the upper bound is below it. The current [meets_floor implementation](../packages/core/src/interviewmaxxing_core/discovery.py) returns `True`, `False`, or `None`; the importer adds corresponding notes. Published base pay is not automatically total compensation.

**Separate canonical identity from a conservative duplicate hold.** Canonical cross-source merging uses proven employer ATS keys and source posting identities. A generic application URL, matching company/title, or similar location does not prove the same vacancy. The task importer additionally normalizes company and title to hold likely repeat roles before saving; that is a conservative review hold, not evidence for a canonical merge. Review genuine separate requisitions manually rather than automatically widening or merging them. Always use `JobStore.upsert`'s returned ID for tracking.

The current [ATS-key helper](../packages/jobs/src/interviewmaxxing_jobs/text.py) covers specific Greenhouse, Lever, Ashby, SmartRecruiters, and Workday URL shapes. The run identified helper gaps for Snap's `myworkdaysite` and Sage's Salesforce-hosted careers pages. The present Workday matcher expects `*.wdN.myworkdayjobs.com`; it does not establish identity for every Workday-branded URL. Preserve source-specific identity and hold possible duplicates when no supported proven employer key is available. These gaps are findings for future implementation, not fixes claimed here.

## Grounded command patterns

Run from the repository root. These are documented patterns, not instructions to restart the active collectors. The task scripts use fixed output names and some hard-coded local endpoints/paths; inspect them before a later run.

```bash
# Inspect the active timer/target without resetting them.
cat .imx/austin-100/run.json

# Preview a parent-curated artifact after exclusions and duplicate review.
uv run --no-sync python .imx/austin-100/save_verified.py \
  .imx/austin-100/final-curated-batch.json

# Within the user's authorized save-only scope, perform that reviewed import.
uv run --no-sync python .imx/austin-100/save_verified.py \
  .imx/austin-100/final-curated-batch.json --write

# Existing J1 CLI interfaces for inspecting stored results and run metadata.
uv run --no-sync python -m interviewmaxxing_jobs listings --limit 20
uv run --no-sync python -m interviewmaxxing_jobs runs
```

The importer currently calls `http://127.0.0.1:8765` with origin `http://127.0.0.1:4317` and explicitly selects the canonical local jobs database. Source collectors instead stage into task-local files/databases and isolated homes. Verify those destinations before reusing the scripts; setting an unrelated environment variable does not override an explicit path in the importer.

The documented mutation sequence inside `save_verified.py` is:

```python
listing = JobListing.model_validate(reviewed_row["listing"])
stored = store.upsert(listing, raw=review_evidence, run_id=run_id)
tracked = api(f"/jobs/{stored.id}/track", {})
# Read the new entry, then update its fields with the latest revision.
api(f"/pipeline/entries/{pipeline_id}", {"revision": revision, "fields": fields})
```

For final accounting, use the same read-only invariant as the importer, then count the current lane rather than receipt length:

```python
original = {e["id"]: e for e in baseline["entries"]}
current = {e["id"]: e for e in pipeline["entries"]}
assert all(current.get(key) == value for key, value in original.items())
new_saved = [e for key, e in current.items()
             if key not in original and e["lane"] == "saved"]
# Completion still requires duplicate/eligibility review, not just this count.
```

Do not call application-creation routes, upload a resume, fill an employer form, or send messages as part of this workflow. No profile, contact, resume, or existing personal pipeline contents belong in acquisition evidence or this runbook.

## Known limits and implementation follow-ups

Current J1 still documents background LinkedIn limitations and a Google result-container click flow with partial descriptions. The foreground continuation and share-URL/expansion collectors under `.imx/austin-100` are task-local workarounds; production integration and tests are not established by their success here. Source markup and availability can change, so re-read live identity and completeness on each run.

Source budgets can stop collectors before the overall target: for example, the LinkedIn helper can stop at its candidate cap and mark its source file complete while the top-level timer still says running. Do not equate that status with 100 Saved. Unknown employer availability and partial-description review flags remain material limitations, and additional acquisition must preserve the original scope rather than quietly admitting remote, low-pay, expired, or semantically unrelated jobs.

The run also exposed three persistence/UI issues for future implementation. First, historical importer receipt length can exceed actual Saved membership: aliases may generate multiple receipts, while later exclusions remain in history. The final canonical projection contains exactly 100 current Saved IDs; the original import history was preserved. Second, a partial supplemental observation should not overwrite richer existing notes or downgrade reviewed evidence; select refreshes deliberately. Third, the open dashboard required a reload after external API writes to display the current count. The final fresh snapshot proved `Saved 100`; the initial page snapshot could capture only a loading state. None of these task-level checks constitutes a shipped product fix.

This run used parallel agent review and a local importer. It did not benchmark Jev, OpenRouter model latency, autonomous job selection, application throughput, or thousands of submissions per day. Source browser sessions were released after completion. All work stayed within the find-and-save boundary.

The new skills encode these recurring workflow triggers; production source-adapter and ATS-parser changes remain separate implementation work:

- **Timed find-and-save intake:** establish explicit geography/attendance, role semantics, pay policy, target, timer, save-only authority, and immutable baseline; retain progress across restarts.
- **Source rendering recovery:** trigger on hidden-page partial rendering or a missing/mismatched detail pane; inspect state, recover only the owned session, and prove identity/completeness before continuing.
- **Semantic job approval:** separate raw acquisition from human-reviewed eligibility, preserve contradictions and exclusion reasons, and prevent provisional flags from reaching the importer.
- **Verified save and reconciliation:** preview approved artifacts, canonicalize proven identities, hold ambiguous duplicates, track locally, attach revision-safe notes, and verify current Saved membership plus baseline preservation.
- **ATS identity extension:** add supported URL-shape fixtures for observed Snap/Sage gaps, with explicit negative cases for generic endpoints; do not infer keys from brand names alone.

For a later run, define completion as verified current Saved membership, not observations, approved rows, historical receipts, or a collector's local status. Preserve the original timer, stop it only after the requested count and UI state are verified, and deliver the count together with pay and availability limitations. The final receipt for this run records that boundary explicitly.


## Follow-up run: 500 additional jobs, September 22–23

The second run saved 500 new listings and finished with 600 in Saved. The original 118 pipeline entries remained unchanged. Its original timer ran from 2026-09-23T04:05:27Z to 2026-09-23T05:24:32.344982Z: **79m 05s**. This missed the requested 44-minute total-time benchmark. The result contains 48 Austin-area onsite/hybrid listings (including one Cedar Park role) and 452 US-remote listings. Of the 500, 235 have an employer-published salary lower bound of at least $100k; 265 require compensation review and 84 require availability review. Saved research leads are not all confirmed-current, salary-qualified openings.

Private evidence is in `.imx/find-500/final-receipt.json`, `coordination/AUDIT_COMPLETE.json`, and `ui/final-snapshot.txt`. Acquisition and semantic review ran in 30 Claude Opus 5.5 Superset terminals; Codex coordinated and verified the pipeline. The source and review skills were supplemented by `find-jobs-fast-batches`, installed for both agents.

Lessons for another run:

- Superset's agent-model whitelist rejected the new Claude model even though the installed Claude CLI accepted it. The working path was a Superset terminal running the verified Claude CLI. Preserve the returned `terminalId` and inspect actual output; terminal existence does not establish progress.
- Native browser contention caused timeouts and dropped connections. After evidenced failures, workers used public job pages and employer ATS data. Unique owned sessions and the fast-batches queue avoid collisions; never route around authentication or challenges.
- Two reviewers could not keep up with 26 finders. Reassigning four low-yield finders to disjoint review queues cleared the backlog. Record ownership and explicitly allow earlier completed reviews when queues move. Keep one canonical writer.
- Combine reviewed inputs, but keep save plans manageable (about 50 SAVE actions). A 213-SAVE plan held the run lock for roughly 28 minutes, making ordinary report counts stale. Label ledger-derived progress provisional until live reconciliation.
- Six durable writes per saved job repeatedly serialized a growing audit ledger. Compact formatting alone reduced bytes by only 8.3% and showed no measurable write-time gain. The reviewed helper update now stores new non-SAVE decisions as references to full hash-pinned plans and drops pre-track card lists only after verified saves. All durable checkpoints remain; 43 tests passed. Preserve plans and reviewed inputs. A separate migration draft was not applied to the completed run.
- Stop expanding acquisition when the reviewed, deduplicated replacement pool is sufficient. Completed task resources and repeated audits added host pressure; this 24 GB machine reached about 19 GB of swap usage.
- A retitled Zello listing duplicated an existing Closed application. Two other saved listings had expired source dates without current employer confirmation. The sole writer corrected only those three run-created cards and saved replacements, preserving the baseline. An expired marketplace date supports exclusion pending verification, not a claim that the employer certainly closed the vacancy.
- All 539 assigned availability checks and two alias-audit slices returned dispositions. Unreadable pages remained explicitly unverified. Reload the dashboard at completion: its earlier snapshot can be stale. Redirect large CLI snapshots to private files and print only the observed Saved heading/count. Wait on exact process IDs or tool handles; broad `pgrep -f` patterns can match the waiting shell itself.
