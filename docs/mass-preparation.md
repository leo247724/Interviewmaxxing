# Mass preparation

`interviewmaxxing prepare-batch` prepares many Saved jobs from an application URL
inventory. Each job is run through the ordinary `apply` command in its own process,
so the canonical runner, the store claims, the pinned resume, the durable no-submit
restriction and the evidence apply unchanged. The harness adds bounded parallel
workers, a resumable ledger and a readiness summary. It does not add a way to submit.

## What it does and does not do

- **Never submits.** Every job runs with the preparation-only runner. A complete
  form stops at the final review step as `NEEDS_INPUT` with a `preparation.ready`
  event (outcome `prepared`). There is no flag in `prepare-batch` or `apply` that
  enables submission; the store also rejects a submission attempt for these
  applications, whatever runs them later.
- **One Chromium per worker slot.** Worker `N` runs with
  `IMX_BROWSER_DIR=$IMX_HOME/browser-workers/wN`, because the runner holds one OS
  lock per browser profile. All workers share the same `IMX_HOME`, state database,
  candidate profile and artifacts directory (each passed explicitly, so per-path
  overrides are kept). A sign-in stored in one worker profile is not visible to
  the others or to the default `$IMX_HOME/browser` profile.
- **Always headless.** Sign-in, CAPTCHA and custom controls therefore stop the
  application as `NEEDS_INPUT` (outcome `needs_input`), exactly as `apply --headless`
  does. Finish them afterwards, one at a time, with
  `interviewmaxxing resume APP --act` in a visible window.
- **Missing answers stop early.** Required questions the verified profile cannot
  answer (facts, saved answers with the exact question wording, consent,
  attestations, salary) come back as `needs_input` with the questions recorded;
  answer them with `interviewmaxxing answer APP` and `resume APP`.
- **OpenCLI is limited to one worker.** `--browser opencli` drives one owned Chrome
  session and leaves each prepared review tab open; `--workers` must be `1`.
- **Nothing private is printed.** Progress lines show outcome, backend, company,
  title, duration and application id. Questions and CLI messages are only written
  to the ledger under `IMX_HOME`.

Preparation runs the site's intermediate steps: a multi-step form may store a
draft on the employer's side before the final step, as with a single `apply`.

## Inventory format

A JSON list of objects (an export of `public.application_urls`, see
[application-url-inventory.md](application-url-inventory.md)). Only these keys are
read; others are ignored:

```json
[
  {
    "listing_id": "lst_123",
    "pipeline_id": "pip_45",
    "company": "Example Co",
    "title": "Growth Marketing Manager",
    "source_application_url": "https://boards.example/example-co/jobs/123/apply",
    "backend": "greenhouse",
    "status": "resolved"
  }
]
```

Rows are filtered by `status` (`resolved` by default) and optionally by `backend`,
ordered by backend and then by their position in the file, then cut to `--limit`.
A row whose `source_application_url` is missing or not an http(s) URL is skipped and
counted as `skipped_invalid_url`. `listing_id` identifies a row in the ledger; a
row without one is keyed by its normalized URL.

## Command

```sh
interviewmaxxing prepare-batch --inventory inventory.json \
  --backends greenhouse,lever --limit 40 --workers 3 --max-prepared 20 \
  [--statuses resolved] [--retry-retryable 1] [--per-job-timeout 900] \
  [--batch-id ID] [--include-existing] [--candidate ID] [--home DIR] [--json]
```

| Option | Meaning |
| --- | --- |
| `--workers N` | Concurrent applications, 1..8, each in its own browser profile. |
| `--max-prepared N` | A hard bound on `prepared` applications in the batch, so the number of review pages waiting for you stays manageable. A job is launched only while the prepared count plus the jobs still running is below `N`. |
| `--retry-retryable N` | Extra attempts (0..2, default 1) for jobs that ended `failed_retryable` or `error` when the same batch id is run again. |
| `--per-job-timeout S` | A job running longer than this is stopped (its whole process group) and recorded as `error`; nothing is submitted. |
| `--batch-id ID` | Name of the batch (default: `batch-YYYYmmddTHHMMSSZ`). Reuse it to resume. |
| `--include-existing` | Also run URLs that already have an application in the store. Without it, such rows are recorded as `already_recorded` and skipped, unless the stored application is `REQUESTED`, `INSPECTING` or `FAILED_RETRYABLE`, which `apply` resumes anyway. |
| `--json` | Print the summary as JSON (progress lines then go to stderr). |

The runtime flags of `apply` are accepted and passed to every job unchanged:
`--browser`, `--opencli-profile`, `--ai-routing`, `--env-file`, `--writer-model` and
`--rag-connection-file` (see [dynamic-runtime.md](dynamic-runtime.md)). The batch
uses the candidate from `--candidate` or `IMX_CANDIDATE_ID`.

Exit status: `0` when the batch has at least one recorded job, `1` when nothing was
recorded, `2` for a usage or validation error, `130` when interrupted. Ctrl-C or
SIGTERM stops the running jobs (nothing is submitted; their applications stay
resumable) and keeps the ledger.

## Outcomes

| Outcome | Stored state | Meaning |
| --- | --- | --- |
| `prepared` | `NEEDS_INPUT` | Final review step reached; `preparation.ready` recorded; nothing submitted. |
| `needs_input` | `NEEDS_INPUT` | Stopped earlier for answers, sign-in, CAPTCHA or a custom control. |
| `failed_retryable` | `FAILED_RETRYABLE` | Browser error, ambiguous next/submit control, a loop, or the browser profile was busy. Retried on rerun. |
| `closed` | `FAILED_PERMANENT` | The job no longer accepts applications. |
| `duplicate` | `DUPLICATE` | The site or the store already has this application. |
| `blocked` | a submission state | Cannot happen in preparation; classified for completeness. |
| `error` | — | The CLI printed no readable outcome, or the job timed out. Retried on rerun. |
| `already_recorded` | as stored | Skipped: an application for this URL already exists (see `--include-existing`). |

## Files

Everything lives under `$IMX_HOME/batches/<batch id>/` (directory `0700`, files `0600`):

- `ledger.jsonl`: one JSON line per finished or skipped job, appended as it happens:
  the row, `attempt`, `worker_slot`, `application_id`, `state`, `outcome`, the CLI
  message (truncated), `missing_reasons`, `missing_labels` (the recorded questions,
  truncated), `exit_code`, `started_at`, `finished_at`, `duration_s`.
- `summary.json`: totals by outcome, a backend × outcome table, median and p95 job
  duration, the most frequent missing questions and reasons, application ids by
  outcome, and the counts of rows skipped as already settled or for an invalid URL.
  Rewritten at the end of every run of the batch. The same summary is printed as
  Markdown (or JSON with `--json`).

Evidence for each application (screenshots of the filled steps and of the review
page) is under `$IMX_HOME/artifacts/APP/`, as for a single `apply`.

## Resuming and retrying

Run the same command with the same `--batch-id`. The latest ledger entry of each
row decides: rows that ended `prepared`, `needs_input`, `closed`, `duplicate`,
`blocked` or `already_recorded` are skipped; rows that ended `failed_retryable` or
`error` run again while their attempts so far do not exceed `--retry-retryable`.
Rows added to the inventory since are run normally. `--max-prepared` counts the
`prepared` outcomes already in the ledger.

A job stopped by the timeout leaves its application with a lapsing claim; the next
`apply` (a rerun of the batch) or `resume APP` takes it over once the claim expires
(five minutes at most).

## Reviewing prepared applications

```sh
interviewmaxxing status                # every application and its state
interviewmaxxing status APP            # recorded questions, events, next steps
interviewmaxxing events APP            # includes preparation.ready with the review step
interviewmaxxing answer APP --set FIELD=VALUE ...   # then: interviewmaxxing resume APP
interviewmaxxing resume APP --act      # visible window for sign-in, CAPTCHA, custom controls
```

`summary.json` lists the application ids per outcome, so a script or the dashboard
can pick up the `prepared` ones. Evidence lives under `$IMX_HOME/artifacts/APP/`.
Resuming a prepared application re-inspects the site and stops at the same review
step again; submission stays disabled for it.

Verification: `tests/core/test_batch.py` runs the harness offline against a fake
`apply` command; `e2e/test_batch_e2e.py` runs it with three workers, real headless
Chromium and the fictional candidate against the localhost mock ATS and checks that
the server received no submission.
