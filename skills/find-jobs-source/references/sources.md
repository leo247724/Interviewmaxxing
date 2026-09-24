# Source commands and recovery

Run from the selected Interviewmaxxing checkout using its working Python environment. Check current CLI help if options differ. These commands acquire into staging; they do not create Saved cards.

## Shared acquisition

```bash
opencli doctor
opencli profile list
```

Select the connected profile from the actual output. `IMX_PROFILE_DIR` is a candidate-data directory, not an OpenCLI browser profile. Never copy a profile ID from a previous run.

With task variables set to verified values:

```bash
umask 077
mkdir -p "$imx_run_dir/staging"
"$imx_python" -m interviewmaxxing_jobs search \
  --query-file "$imx_run_dir/query.json" \
  --sources "$imx_source" --no-remote \
  --profile "$imx_browser_profile" --window foreground \
  --db "$imx_run_dir/staging/$imx_source.sqlite3" \
  --limit 150 --details 75 --max-pages 3 \
  > "$imx_run_dir/$imx_source-$imx_batch-run.json"
```

Use one of `linkedin`, `google`, or `builtin` for `imx_source`; one worker per source. Set `imx_batch` to a fresh batch identifier so summaries are not overwritten. A multi-source native search executes sequentially. Use separate processes and staging files for different sources. Same-source calls remain sequential, even from different worktrees. Adjust acquisition budgets to observed yield; 150 observations are not 150 eligible jobs. Use smaller searches when early batch handoff is more useful than one long collector call. Remove `--no-remote` and set the query's remote target only when the actual request includes remote work.

Use the [example query](../../find-jobs/assets/austin-performance.query.json) only when its scope matches. For future custom queries, validate `JobSearchQuery` before browser work. `--db` is an absolute SQLite file path; its constructor does not expand a literal `~`. `--db` overrides environment defaults.

Export native objects from staging using `JobStore(path).list_listings()` and each listing's `model_dump(mode="json")`. Retain search state from the run output and observation records from `store.observations(listing.id)`. The `listings` CLI display intentionally omits complete descriptions/provenance and is not a valid review/import envelope.

## LinkedIn

- Use the owned foreground session. Hidden/background tabs can render cards without the full description. Inspect visibility and completeness before adding waits; foreground only the owned job tab.
- Native searches group up to four unquoted title seeds with OR. Austin onsite/hybrid searches use work-type values `1,3`. Seeds broaden discovery; review still decides fit.
- Visit job-specific links from observed numeric posting IDs. Verify displayed company/title and source identity before extracting the description. Preserve public-page access and sign-in blockers.
- Do not use installed `opencli linkedin search`: it uses private Voyager calls outside this project's visible-page transport. Never borrow an assessment or unrelated user session.

## Google Jobs

- Use Jobs results (`udm=8`) and vary a few relevant search legs to increase coverage. Native search does not establish exhaustive pagination.
- Result-container clicks can leave another job's detail pane active. Read the selected result's observed `share_url`, navigate to it in the owned session, wait for the selected pane, then verify its ID/title/company.
- If the matched pane has **Show full description**, locate its unique current control, use a structured click, refresh state, and recheck identity and expansion. Use readiness evidence; fixed delays alone do not prove completion.
- Preserve the original employer title/pay when syndicated titles or estimates differ. An aggregator salary estimate remains raw text, not employer numeric compensation.
- Share-URL navigation and expansion were verified as task-level recovery patterns. The native Google adapter still needs this manual recovery in affected cases; do not claim it has been fixed merely because the workflow succeeds.

## Built In

- Native searches use keywords. Supplement with category browsing when appropriate:
  `https://builtin.com/jobs/hybrid/office/marketing?city=Austin&state=Texas&country=USA&allLocations=true`
- Follow observed next-page links, collect unique job IDs, and inspect descriptions only after cheap duplicate checks.
- Read `validThrough`, closure text, employer identity, and actual office attendance. Search presence and a recent-looking card are not currentness proof.
- Prefer employer evidence when a card's Austin or office label contradicts a remote-first job or another city's required office. Optional coworking access alone does not establish a hybrid role.

## Employer verification and failures

Use observed employer career links to settle closed, expired, remote, salary, and identity contradictions. A dead syndicated copy does not prove a different current requisition is dead; verify and record the new identity. A filled employer posting does override a still-visible marketplace card. Keep unknown availability explicitly unknown.

Keep partial observations if acquisition fails. Report PARTIAL, BLOCKED, ERROR, or NEEDS_USER with the exact actionable cause; never turn an authentication wall into zero successful results. Retry once after a concrete state change, then change the acquisition route or return the blocker instead of looping. Do not bypass authentication or bot challenges.
