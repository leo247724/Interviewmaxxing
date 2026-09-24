# Mass preparation

`interviewmaxxing prepare-batch` prepares many Saved jobs from an application URL
inventory. Each job is run through the ordinary `apply` command in its own process,
so the canonical runner, the store claims, the pinned resume, the durable no-submit
restriction and the evidence apply unchanged. The harness adds bounded parallel
workers, a resumable ledger, a readiness summary and links from the Saved pipeline
cards to the resulting applications. It does not add a way to submit.
`interviewmaxxing batch-report` summarizes one or more batch ledgers afterwards.

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
- **Saved cards are linked.** A row with a `pipeline_id` has its application linked
  to that Saved card once the job's outcome is known. See
  [Pipeline cards](#pipeline-cards).
- **A closed job's card moves to Closed.** When the job no longer accepts
  applications, its linked Saved card moves to Closed with a dated note in the
  card's history. `--no-sync-closed` turns this off.
- **Cards are never created or advanced.** The batch creates no card and no
  pipeline database, and never moves a card to Applied. Closed is the only lane it
  moves a card to.
- **Nothing private is printed.** Progress lines show outcome, backend, company,
  title, duration, application id and, for a row with a card, `[card linked]`,
  `[card not linked]` or `[card linked, moved to Closed]`. Questions and CLI
  messages are only written to the ledger under `IMX_HOME`; a closed job's reason
  also goes into its card's history note in the local pipeline database.

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
    "pipeline_id": "pipe_45",
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
row without one is keyed by its normalized URL. `pipeline_id` is the id of the
job's Saved card (`pipe_...`), used to link the application to it (see
[Pipeline cards](#pipeline-cards)); a row without one is prepared but not linked.

## Command

```sh
interviewmaxxing [--home DIR] prepare-batch --inventory inventory.json \
  --backends greenhouse,lever --limit 40 --workers 3 --max-prepared 20 \
  [--statuses resolved] [--retry-retryable 1] [--per-job-timeout 900] \
  [--batch-id ID] [--include-existing] [--sync-closed | --no-sync-closed] \
  [--candidate ID] [--json]
```

| Option | Meaning |
| --- | --- |
| `--workers N` | Concurrent applications, 1..8, each in its own browser profile. |
| `--max-prepared N` | A hard bound on `prepared` applications in the batch, so the number of review pages waiting for you stays manageable. A job is launched only while the prepared count plus the jobs still running is below `N`. |
| `--retry-retryable N` | Extra attempts (0..2, default 1) for jobs that ended `failed_retryable` or `error` when the same batch id is run again. |
| `--per-job-timeout S` | A job running longer than this is stopped (its whole process group) and recorded as `error`; nothing is submitted. |
| `--batch-id ID` | Name of the batch (default: `batch-YYYYmmddTHHMMSSZ`). Reuse it to resume. |
| `--include-existing` | Also run URLs that already have an application in the store. Without it, such rows are recorded as `already_recorded` and skipped, unless the stored application is `REQUESTED`, `INSPECTING` or `FAILED_RETRYABLE`, which `apply` resumes anyway. |
| `--sync-closed`, `--no-sync-closed` | On by default. When a `closed` row (or an `already_recorded` row whose stored state is `FAILED_PERMANENT`) has a linked card still in Saved, moves the card to Closed through the revision-aware `move_item` with the note `Observed closed on YYYY-MM-DD (UTC) by prepare-batch <batch id>: <observed reason> (application <id>)`. The note appears in the card's history ("Moved from Saved to Closed. …"). No other card field changes. A card already in Closed is left as it is, with nothing recorded; a card in any other lane is left where it is. `--no-sync-closed` still links cards but moves none. |
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

## Pipeline cards

A row's `pipeline_id` names the job's Saved card. After each job's outcome is known,
and before its ledger line is written, the batch links the application to that
card. `already_recorded` rows are linked too, so running the inventory again under a
new `--batch-id` links (and, for closed jobs, closes) the cards of applications that
earlier batches recorded, without launching those jobs again. A link is made only
when:

1. The job has an application id.
2. The application is the canonical one for the row's URL: the one the service's
   `record_request` returns for that URL. That is the run's own application, or
   the surviving application when the run's application is a `DUPLICATE` of it;
   the survivor is then the one linked.
3. The card exists for the candidate, belongs to the row's listing when both
   listing ids are known, and is unlinked or already linked to that application. An
   existing link to another application is never overwritten.
4. The link is written with the revision-aware `update_item` and
   `PipelineUpdate(application_id=...)`. After a concurrent edit the batch re-reads
   the card and tries again, up to three attempts. No other card field changes.
5. Linking is idempotent: a card already linked to the application is not written
   again, and its revision does not change.

For a closed job (outcome `closed`, or `already_recorded` with the stored state
`FAILED_PERMANENT`), unless `--no-sync-closed` is given, a card that is linked to
the application and still in the Saved lane (id `saved`, or labelled Saved) moves to
the Closed lane (id `closed`, or labelled Closed) through the revision-aware
`move_item`, retried after a concurrent edit in the same way. The move is added to
the card's append-only history with a note such as:

```text
Observed closed on 2026-09-24 (UTC) by prepare-batch batch-20260924T090000Z: The job is no longer accepting applications. (application app_example)
```

The date is when the job finished (for `already_recorded`, when the stored
application was last updated), in UTC. The reason is the `closed` job's CLI message
or, for `already_recorded`, the stored application's failure reason, cut to 200
characters; `The job no longer accepts applications.` when there is none. The
application id is the linked one. The dashboard shows the entry as
"Moved from Saved to Closed. Observed closed on …". Tracking fields, notes, listing
id, application URL and selection stay as they were. A card already in Closed (moved
by an earlier batch or by you) is left as it is: no note is added and `closed_synced`
stays `null`, so a rerun records no problem. A card in any other lane is left there,
and the ledger says why.

When a card is not linked or not moved, the ledger line says why:

| Field | Value | Meaning |
| --- | --- | --- |
| `link_reason` | `no application id` | The job produced no application (for example an `error` before `apply` recorded one). |
| `link_reason` | `pipeline package not installed` | `interviewmaxxing_pipeline` cannot be imported. |
| `link_reason` | `no pipeline database` | The pipeline database beside the state database (by default `$IMX_HOME/state/pipeline.sqlite3`) does not exist. A batch never creates it. |
| `link_reason` | `application not found` | The application is not in the store for this candidate, or there is no state database. |
| `link_reason` | `application does not match the row's URL` | The store's application for the row's URL is neither this application nor the one it duplicates. |
| `link_reason` | `card not found` | The candidate has no card with this id. |
| `link_reason` | `card is for another listing` | The card's `listing_id` and the row's `listing_id` are both known and differ. |
| `link_reason` | `card links another application` | The card already links a different application; that link is kept. |
| `link_reason`, `closed_sync_reason` | `card kept changing` | The card changed before each of three attempts; nothing was written. |
| `link_reason` | `link error: <type>` | An unexpected error, named by its exception type. |
| `closed_sync_reason` | `card not linked` | The card is not linked to the application, so it is not moved. |
| `closed_sync_reason` | `board has no Closed lane` | The candidate's board has no lane with id `closed` or label Closed. |
| `closed_sync_reason` | `card not in Saved (in <lane id>)` | The card is in a lane other than Saved or Closed, for example `(in applied)`. It stays there. |
| `closed_sync_reason` | `move error: <type>` | An unexpected error while moving, named by its exception type. |

A settled row is not launched again when the same batch id runs again, so a link
that failed on it is not retried either. To retry the link, run the inventory
under a new `--batch-id`. A row that runs again (`failed_retryable`, `error`) is
linked when it finishes.

Limitations: listing aliases are not resolved, so a card whose `listing_id` differs
from the row's is refused (`card is for another listing`) even when one listing was
merged into the other. The batch does not compare the saved listing's employer job
key with the application's observed job identity; the service's application
handoff does. The inventory's pairing of listing, card and URL is trusted as
exported.

## Files

Everything lives under `$IMX_HOME/batches/<batch id>/` (directory `0700`, files `0600`):

- `ledger.jsonl`: one JSON line per finished or skipped job, appended as it happens:
  the row, `attempt`, `worker_slot`, `application_id`, `state`, `outcome`, the CLI
  message (truncated), `missing_reasons`, `missing_labels` (the recorded questions,
  truncated to 120 characters), `missing_items` (each recorded question or action
  in order, with its `label`, `reason` and `control_type`), `exit_code`,
  `started_at`, `finished_at`, `duration_s`, and the card fields below. Lines
  written before `missing_items` and the card fields existed still parse.
  - `linked`: `null` when the row has no `pipeline_id` (or the line predates
    linking), else `true` or `false`.
  - `link_reason`: when `linked` is `false`, why (see
    [Pipeline cards](#pipeline-cards)).
  - `linked_application_id`: when `linked` is `true`, the application the card
    links.
  - `closed_synced`: `true` when this run moved the card from Saved to Closed,
    `false` when it could not; `null` when the job is not closed, the row has no
    card, `--no-sync-closed` was given, or the card was already in Closed.
  - `closed_sync_reason`: why the card was not moved, when `closed_synced` is
    `false`.
- `summary.json`: totals by outcome, a backend × outcome table, median and p95 job
  duration, the most frequent missing questions and reasons, application ids by
  outcome, the counts of rows skipped as already settled or for an invalid URL, and
  the card counts over each row's latest entry: `pipeline_linked`,
  `pipeline_not_linked`, `pipeline_closed` (moved to Closed) and `pipeline_problems`
  (the reasons for cards not linked and Closed moves skipped, counted; a move
  skipped because the link failed counts once, under the link reason). Rewritten at
  the end of every run of the batch. The same summary is printed as Markdown (or
  JSON with `--json`); the Markdown has a `pipeline cards` line only when some row
  has a card.

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

## Batch report

```sh
interviewmaxxing [--home DIR] batch-report [BATCH_ID] [--top N] [--json]
```

Reads `$IMX_HOME/batches/<BATCH_ID>/ledger.jsonl`, or every batch ledger under
`$IMX_HOME/batches/` when no id is given, and prints one report for them. It writes
no file and creates nothing, not even `$IMX_HOME`. Only for ledger lines older than
`missing_items` does it open the existing state database, the same way `status`
does (an idempotent schema check).

Each listing counts once, as its latest launched entry (any outcome except
`already_recorded`) in the selected ledgers, else its latest `already_recorded`
entry. Latest is by `finished_at`; ties go by batch id order, then line order. The
report shows:

- **Totals** by outcome and a **backend × outcome** table for those rows. A row
  without a backend is counted as `(none)`.
- **Durations** per backend: the number of launched attempts and their median and
  p95 in seconds, over every launched attempt in the selected ledgers, plus the
  same over all backends.
- **Holds by category**: the questions and actions that stopped those rows. For
  each category: the number of holds, the number of applications, the most frequent
  questions (at most `--top N`, 1..100, default 10) and the application ids.
  Categories with the most holds come first; empty ones are left out.
- **Pipeline cards**, when some row has a card: rows with a card, linked, not
  linked, moved to Closed, not moved, and the reasons counted as in `summary.json`.
  For each listing, its latest entry with a link result counts as linked or not
  linked, and its latest entry with a Closed-move result counts as moved or not.

`--json` prints the same report as JSON. Hold categories:

| Category | Rule (checked in this order) |
| --- | --- |
| `custom_control` | Reason `UNSUPPORTED_CONTROL`. |
| `explicit_answer` | Reason `EXPLICIT_ANSWER_REQUIRED` or `UNCOVERED_ATTESTATION`. |
| `lookup` | Reason `NO_ANSWER` on a `TYPEAHEAD` control. |
| `screener_yes_no` | Reason `NO_ANSWER`, and the question starts with the words "do you", "have you" or "are you" (ignoring case, spacing and leading marks such as `*`, `✱` or quotes). |
| `narrative` | Reason `NO_ANSWER`, and the question contains the word "describe", "tell", "why" or "explain". |
| `other` | Anything else, including `AMBIGUOUS`, `USER_ACTION` and an unknown reason. |

A hold's reason and control type come from the ledger's `missing_items`. A ledger
line written before that field existed has only `missing_labels` and
`missing_reasons`. For such a line, when the state database exists, each label is
looked up among the application's recorded questions (those of its latest
`NEEDS_INPUT` event, then those of its latest packet), matched by the
120-character label; a match gives the reason and the control type. A label not
found there takes the line's reason when the line has exactly one, and otherwise
counts as `other`.

The ledger keeps the full question labels (cut to 120 characters) under
`$IMX_HOME`. The report prints them cut to 80 characters, in text and JSON, and
never prints CLI messages.

Exit status: `0` on success, including when there are no ledgers (the report then
says `No batch ledgers under <dir>.`), `1` when the named batch has no ledger, and
`2` for an invalid batch id.

To work through a category, open its listed applications one at a time and
continue with `answer` and `resume` as below:

```sh
interviewmaxxing batch-report --top 5            # every batch
interviewmaxxing status app_example              # its recorded questions and next steps
```

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
`apply` command, including card links and Closed moves with a store-backed fake;
`tests/core/test_batch_report.py` covers `batch-report`;
`tests/service/test_application_links_batch.py` checks that the service accepts the
links a batch writes and shows its Closed moves; `e2e/test_batch_e2e.py` runs the
harness with three workers, real headless Chromium and the fictional candidate
against the localhost mock ATS and checks that the server received no submission.
