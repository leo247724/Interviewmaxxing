# Run helper

Use the checkout's Python environment, with `interviewmaxxing_core` and `interviewmaxxing_jobs` importable. Resolve the script relative to the loaded `find-jobs/SKILL.md`, not relative to the working directory. The installed skill can be shared across worktrees; its Python dependencies come from the selected checkout.

Set task-specific shell variables to verified absolute paths and URLs. The examples below assume `imx_python`, `imx_skill_dir`, `imx_run_dir`, `imx_jobs_db`, `imx_api_url`, and `imx_origin` have been resolved. Never repurpose `HOME` or `CODEX_HOME`. Keep run artifacts private (`umask 077`) and outside version control.

```bash
"$imx_python" "$imx_skill_dir/scripts/find_jobs.py" --help
"$imx_python" "$imx_skill_dir/scripts/find_jobs.py" schema

"$imx_python" "$imx_skill_dir/scripts/find_jobs.py" init \
  --run-dir "$imx_run_dir" --scope "$imx_run_dir/scope.json" --target 100 \
  --jobs-db "$imx_jobs_db" --api-url "$imx_api_url" --origin "$imx_origin"

"$imx_python" "$imx_skill_dir/scripts/find_jobs.py" index --run-dir "$imx_run_dir"
```

Replace `100` with the user's requested target. Create `scope.json` from the current request before initialization. Location fragments are case-insensitive word matches against an observed location string; they are not a geocoder or proof of actual office attendance. The reviewer must check the underlying location and arrangement evidence. `role_focus` is also reviewed semantically, not proved by the helper.

For each completed review batch:

```bash
"$imx_python" "$imx_skill_dir/scripts/find_jobs.py" preview \
  --run-dir "$imx_run_dir" "$imx_run_dir/reviewed/linkedin-01.json"

# Use the exact plan path returned by preview.
"$imx_python" "$imx_skill_dir/scripts/find_jobs.py" save \
  --run-dir "$imx_run_dir" --plan "$imx_plan_path"

"$imx_python" "$imx_skill_dir/scripts/find_jobs.py" report --run-dir "$imx_run_dir"
```

The helper validates every input before mutation, holds duplicates and existing tracked entries, enforces the remaining target capacity, upserts through native `JobStore`, uses the returned canonical ID, checks that the service sees the same listing, tracks it locally, and writes notes using the entry's current revision. Only the coordinator runs `save`. The only mutation endpoints used are job tracking and pipeline field updates; no application route is needed. HOLD and EXCLUDE decisions stay in private artifacts rather than creating Closed cards.

Keep each run's plans and reviewed inputs intact. New non-SAVE ledger decisions reference the full reviewed record in a hash-pinned plan using its input path, row, and record hash; the ledger is not their only audit artifact. Old full decision records remain readable. The helper writes compact ledger JSON and removes the pre-track card list only after verifying a saved intent, while preserving every durable checkpoint. Do not delete plans, prune a live ledger manually, or assume smaller JSON alone makes saving faster.

If a save is interrupted, rerun using its unchanged plan and original run directory. Inspect `report` and the error first. An acknowledged new card can be reconciled; a lost tracking response plus a matching card does not prove this run created it. On retry the helper holds that intent, releases its reserved target slot, and leaves the ambiguous card unchanged and uncounted. Continue with another distinct candidate; preserve the held intent for separate attribution review. Deterministically invalid pending candidates without an acknowledged owned card are also held so a closed vacancy cannot permanently reserve a slot. Do not delete the ledger, change a protected entry, or fabricate a replacement receipt to clear a failure. If reviewed inputs change, create a new preview. A wrong service/database pairing, a changed baseline, or a stale plan must be corrected explicitly before continuing.

To withdraw an unfinished approval after new semantic evidence, put that candidate's HOLD or EXCLUDE decision in a new reviewed envelope, then preview and save the new plan. The helper retires an untracked pending intent and releases its slot. It refuses to withdraw an already tracked card; that card needs a separate pipeline correction rather than a ledger edit or a silent move.

After reloading and verifying the actual dashboard:

```bash
"$imx_python" "$imx_skill_dir/scripts/find_jobs.py" finish \
  --run-dir "$imx_run_dir" --ui-snapshot "$imx_ui_snapshot" \
  --ui-observed-at "$imx_ui_observed_at" --ui-url "$imx_pipeline_url"
```

Use the genuine snapshot file and its observation time, not a manually authored assertion. The helper can check freshness, the claimed URL, and count text; it cannot prove that a human or browser supplied truthful evidence. The agent must obtain and inspect that state. Successful completion freezes the timer and writes a receipt. A source finishing, a process exiting, or a partial count must not finish the overall timer.

## Reviewed input contract

Use `schema` for the executable contract and native `JobListing` schema. A reviewed file has this structure:

```json
{
  "schema_version": "imx.find-jobs.reviewed.v1",
  "run_id": "the run ID returned by init",
  "reviewer": "assigned reviewer identifier",
  "records": [
    {
      "listing": {},
      "decision": "SAVE",
      "compensation_basis": "employer",
      "fit_rationale": "Evidence-based fit decision.",
      "evidence": {
        "role": "Observed source URL, time, and relevant duties.",
        "location": "Evidence of the actual work location.",
        "arrangement": "Evidence of required or explicitly available attendance.",
        "availability": "Open, closed, or unknown with supporting observation.",
        "compensation": "Employer wording, or an explicit unknown/estimate label.",
        "identity": "Source posting ID, employer requisition, and duplicate check."
      },
      "review_reasons": [],
      "duplicate_of": null
    }
  ]
}
```

The empty `listing` above is a shape illustration, not valid input. Populate it with the complete native object from source staging and validate it; do not rebuild it from the shortened CLI listings display. `decision` is SAVE, HOLD, or EXCLUDE. `compensation_basis` is employer, estimated, or unknown. Estimated/unknown compensation must not carry numeric bounds; preserve its source wording as raw text. Include URLs and observations in evidence, and explicit review reasons for incomplete descriptions, unknown availability, uncertain pay, qualifications, or unresolved identity. A duplicate is held, even when a collector labeled it eligible.
