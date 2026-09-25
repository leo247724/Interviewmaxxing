# Mass preparation

`interviewmaxxing prepare-batch` prepares many Saved jobs from an application URL
inventory. Each job is run through the ordinary `apply` command in its own process,
so the canonical runner, the store claims, the pinned resume, the durable no-submit
restriction and the evidence apply unchanged. The harness adds bounded parallel
workers, a resumable ledger, a readiness summary and links from the Saved pipeline
cards to the resulting applications. It does not add a way to submit.
`interviewmaxxing batch-report` summarizes one or more batch ledgers afterwards,
`interviewmaxxing holds` lists every open question once with the line that answers it,
and `prepare-batch --retry` runs the held and failed applications of a batch again.

The loop at scale:

```sh
interviewmaxxing prepare-batch --inventory inventory.json --workers 3 --batch-id big1
interviewmaxxing batch-report big1          # outcomes, backends, questions, fill failures
interviewmaxxing holds                      # each open question once, with its answer line
interviewmaxxing answer APP --set FIELD=VALUE --reuse global   # once per question
interviewmaxxing prepare-batch --retry big1 # the held and failed ones again, same settings
interviewmaxxing batch-report big1 big1-retry-20260924T210507Z
```

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
- **Owner-only directories.** The batch creates the local directories first
  (`LocalPaths.ensure`), then `batches/`, the batch directory, `browser-workers/` and
  each slot directory one level at a time, each `0700`; ledgers and summaries are
  `0600`. Directories that already exist keep their mode.
- **One row per job at a time.** Rows whose URLs normalize to the same job (for
  example with and without a trailing slash or a tracking parameter) are not run
  together: the first one runs, the others are counted as `skipped_same_url`, and the
  next run of the batch records them as `already_recorded` against the same
  application (and links their cards).
- **One state-database connection.** The harness's own reads (does the URL already
  have an application, its provider cost, the application a card links to) share one
  connection on one thread (`StateReader`), opened once the database exists, instead of
  opening the store (and running its schema check, a write transaction) two or three
  times per job while other jobs write to it. Lookups and card bookkeeping never run
  under the workers' lock, so a busy pipeline database (the dashboard editing a card)
  delays only its own row, not every launch.
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
- **A closed job's card moves to Closed.** When the run saw the job closed (the
  runner's wording "The job is no longer accepting applications"), its linked Saved
  card moves to Closed with a dated note in the card's history. Any other
  `FAILED_PERMANENT` stop leaves the card where it is. `--no-sync-closed` turns this off.
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

interviewmaxxing [--home DIR] prepare-batch --retry BATCH_ID \
  [--outcomes needs_input,failed_retryable,unknown,error] [--include-explicit] \
  [--backends A,B] [--limit N] [--max-prepared N] [--batch-id ID] \
  [--workers N] [--per-job-timeout S] [runtime flags] [--json]
```

`--inventory` and `--retry` are mutually exclusive; see
[Retrying held and failed applications](#retrying-held-and-failed-applications).

| Option | Meaning |
| --- | --- |
| `--workers N` | Concurrent applications, 1..8, each in its own browser profile. |
| `--max-prepared N` | A hard bound on `prepared` applications in the batch, so the number of review pages waiting for you stays manageable. A job is launched only while the prepared count plus the jobs still running is below `N`. |
| `--retry-retryable N` | Extra attempts (0..2, default 1) for jobs that ended `failed_retryable` or `error` when the same batch id is run again. |
| `--per-job-timeout S` | A job running longer than this is stopped (its whole process group: SIGTERM, then SIGKILL after 15 s) and recorded as `error`; nothing is submitted. The message says how it ended: `the run was stopped (SIGTERM)`, `the run ignored SIGTERM for 15 s and was killed (SIGKILL)`, or `the run did not exit after SIGTERM and SIGKILL (process group N may still be running)`. |
| `--batch-id ID` | Name of the batch (default: `batch-YYYYmmddTHHMMSSZ`; with `--retry`, `BATCH_ID-retry-YYYYmmddTHHMMSSZ`). Reuse it to resume. |
| `--include-existing` | Also run URLs that already have an application in the store. Without it, such rows are recorded as `already_recorded` and skipped, unless the stored application is `REQUESTED`, `INSPECTING`, `PACKET_READY`, `FILLING` or `FAILED_RETRYABLE`, which `apply` resumes anyway (a run stopped mid-fill, for example by a timeout, is left in `PACKET_READY` or `FILLING`). |
| `--sync-closed`, `--no-sync-closed` | On by default. When a `closed` row (or an `already_recorded` row whose stored state is `FAILED_PERMANENT`) saw the job closed, that is its reason is the runner's "The job is no longer accepting applications…", and it has a linked card still in Saved, moves the card to Closed through the revision-aware `move_item` with the note `Observed closed on YYYY-MM-DD (UTC) by prepare-batch <batch id>: <observed reason> (application <id>)`. The note appears in the card's history ("Moved from Saved to Closed. …"). No other card field changes. A `FAILED_PERMANENT` for any other reason moves nothing. A card already in Closed is left as it is, with nothing recorded; a card in any other lane is left where it is. `--no-sync-closed` still links cards but moves none. |
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
| `failed_retryable` | `FAILED_RETRYABLE` | Browser error, ambiguous next/submit control, a loop, or the job did not run because the browser profile or the application was busy ("another run is using the browser profile", "Another run is working on this application.", whatever state the latter reports). Retried on rerun. |
| `closed` | `FAILED_PERMANENT` | The job no longer accepts applications (or, in principle, another permanent failure; only the runner's closed wording moves a card). |
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
`FAILED_PERMANENT`) whose reason is the runner's closed wording ("The job is no longer
accepting applications", the `closed` row's message or the stored application's failure
reason), unless `--no-sync-closed` is given, a card that is linked to the application
and still in the Saved lane (id `saved`, or labelled Saved) moves to the Closed lane (id
`closed`, or labelled Closed) through the revision-aware `move_item`, retried after a
concurrent edit in the same way. A `FAILED_PERMANENT` stop for any other reason is
linked but not moved (`closed_synced` stays `null`): only a job seen closed closes its
card. The move is added to the card's append-only history with a note such as:

```text
Observed closed on 2026-09-24 (UTC) by prepare-batch batch-20260924T090000Z: The job is no longer accepting applications. (application app_example)
```

The date is when the job finished (for `already_recorded`, when the stored
application was last updated), in UTC. The reason is the `closed` job's CLI message
or, for `already_recorded`, the stored application's failure reason, without the
runner's "Provider cost: …" note, cut to 200 characters. The application id is the
linked one. The dashboard shows the entry as
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
  in order, with its `label`, `reason`, `control_type`, `field_id` and
  `semantic_type`), `exit_code`, `started_at`, `finished_at`, `duration_s`, and the
  card, cost and retry fields below. Lines written before any of these fields existed
  still parse; a line this version cannot read (cut short by a crash, edited, or
  written by a newer version) is skipped and counted as `ledger_lines_ignored`, and its
  row counts as unrecorded (it runs again, and `--max-prepared` does not count it). A
  line cut short by a crash is ended before the next one is appended, so the next
  line stays readable.
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
  - `provider_cost_usd` and `provider_calls`: the application's known AI provider
    cost (USD) and number of provider calls so far, summed over every
    `provider.budget` event the runner recorded for it. They are read from the state
    database after the job finishes. `null` when no provider was used, for example
    without `--ai-routing`, or on lines written before these fields existed.
  - `retry_of`: on the lines of a `prepare-batch --retry` batch, the batch it retried;
    `null` otherwise.
  - `previous_outcome`, `holds_before`, `holds_cleared`: for a retry, where the
    application stood when it was selected (`needs_input`, `failed_retryable`,
    `unknown` or `error`), how many questions and actions it was held on then, and how
    many of those no longer hold it: all of them once it is prepared; for
    `needs_input`, those whose wording is not asked again (a question asked twice
    counts twice). `holds_cleared` is `null` when the run ended any other way.
- `summary.json`: totals by outcome, a backend × outcome table, median and p95 job
  duration, the most frequent missing questions and reasons, application ids by
  outcome, the counts of rows skipped as already settled or for an invalid URL, and
  the card counts over each row's latest entry: `pipeline_linked`,
  `pipeline_not_linked`, `pipeline_closed` (moved to Closed) and `pipeline_problems`
  (the reasons for cards not linked and Closed moves skipped, counted; a move
  skipped because the link failed counts once, under the link reason). Also
  `skipped_same_url` (rows not launched because an earlier row of the run has the same
  normalized URL, or in a retry the same application), `ledger_lines_ignored`,
  `run_options` (the run's candidate, workers, per-job timeout, retry count,
  `--max-prepared`, `--include-existing`, closed-card sync, browser and runtime flags,
  which `--retry` reuses) and, for a retry batch, `retry` (see
  [Retrying held and failed applications](#retrying-held-and-failed-applications)).
  Rewritten at the end of every run of the batch. The same summary is printed as
  Markdown (or JSON with `--json`); the Markdown has a `pipeline cards` line only when
  some row has a card, and escapes `|` in table cells.

`interviewmaxxing batch-report` adds a "Provider cost" section when rows carry a cost:
the known total over the rows (each listing once, at its latest line with a cost, so a
retry that crashed before an application id was known does not drop it), the calls,
and the cost per prepared application (total divided by prepared rows). Its JSON
has `provider_cost_usd`, `provider_calls`, `provider_cost_rows` and
`cost_per_prepared_usd`. Costs a provider did not report are not included, so the
total is a lower bound when some calls had none; the runner's `provider.budget` event
counts them as `unknown_cost_calls`. A job that timed out or crashed records no event
for its unfinished run.

Evidence for each application (screenshots of the filled steps and of the review
page) is under `$IMX_HOME/artifacts/APP/`, as for a single `apply`.

## Resuming and retrying

Run the same command with the same `--batch-id`. The latest ledger entry of each
row decides: rows that ended `prepared`, `needs_input`, `closed`, `duplicate`,
`blocked` or `already_recorded` are skipped; rows that ended `failed_retryable` or
`error` run again while their attempts so far do not exceed `--retry-retryable`.
Rows added to the inventory since are run normally, and rows held back because another
row had the same URL are recorded now (`already_recorded`). `--max-prepared` counts the
`prepared` outcomes already in the ledger. To run held applications again after a fix
or after answering their questions, use `--retry` (below): it continues them with
`resume` as a new batch.

A job stopped by the timeout leaves its application with a lapsing claim; the next
`apply` (a rerun of the batch) or `resume APP` takes it over once the claim expires
(five minutes at most).

## Retrying held and failed applications

```sh
interviewmaxxing [--home DIR] prepare-batch --retry BATCH_ID \
  [--outcomes needs_input,failed_retryable,unknown,error] [--include-explicit] \
  [--backends A,B] [--limit N] [--max-prepared N] [--batch-id ID] [--json]
```

After a runtime fix, or after answering questions (see
[Open holds](#open-holds-interviewmaxxing-holds)), `--retry` runs the applications of
an earlier batch that are held or failed again. Each listing of that batch's ledger
counts at its latest line, and is judged by where its application stands **now** in
the state database (a listing without an application id is looked up by its URL):

| Now | Meaning | Retried |
| --- | --- | --- |
| `prepared` | NEEDS_INPUT at the final review step | never |
| `closed` | FAILED_PERMANENT | never |
| `duplicate`, `blocked` | DUPLICATE, a submission state | never |
| `needs_input` | NEEDS_INPUT for questions, sign-in, CAPTCHA or a custom control | with `--outcomes` (default) |
| `failed_retryable` | FAILED_RETRYABLE | with `--outcomes` (default) |
| `unknown` | REQUESTED, INSPECTING, PACKET_READY or FILLING: a run started and recorded no outcome (it timed out or crashed) | with `--outcomes` (default) |
| `error` | the job never recorded an application (for example the CLI could not start) | with `--outcomes` (default); it runs `apply URL` |

A listing whose ledger line says `prepared`, `closed`, `duplicate` or `blocked` is
never retried either, whatever the store says now. A `needs_input` application whose
open holds are all `EXPLICIT_ANSWER_REQUIRED` is skipped unless `--include-explicit`:
only the person can answer those. A hold is no longer open once, after the
application stopped, the person answered exactly that question for it
(`interviewmaxxing answer APP`), or saved an answer whose question is the same wording
(compared as the resolver compares it), whose semantic type is unset or the hold's,
and which applies to the job (`--reuse global`, or `--reuse job` for this job). An
answer that existed before the stop was already available to the run that stopped,
so it does not count. A second listing of an application already selected (an alias
URL) is skipped. `--backends` keeps the listings of those backends; `--limit N` runs
at most N of the selected ones.

The selected applications run as a new batch (`--batch-id`, default
`BATCH_ID-retry-YYYYmmddTHHMMSSZ`; it must differ from `BATCH_ID`) with the same
harness: one `interviewmaxxing resume APP --json --headless` subprocess per
application (`apply URL` for an `error` row without one), per-slot browser profiles,
the per-job timeout, the card bookkeeping, and nothing submitted: `resume` keeps each
application's preparation-only restriction. The worker count, per-job timeout, retry
count, closed-card sync, candidate, browser and runtime flags (`--ai-routing`,
`--env-file`, `--writer-model`, `--rag-connection-file`, `--opencli-profile`) come from
the retried batch's `summary.json` (`run_options`); a flag given with `--retry`
replaces its recorded value, even when it is given with its default value. A batch
whose summary predates `run_options` uses the flags given (a note says so).
`--max-prepared` is the retry's own; `--inventory`, `--statuses` and
`--include-existing` do not apply. The retried batch's ledger is only read.

The retry's ledger lines carry `retry_of`, `previous_outcome`, `holds_before` and
`holds_cleared`, and its `summary.json` a `retry` object: `retry_of`, the `outcomes`
selected, `include_explicit`, `considered` (listings after `--backends`),
`selected`, `skipped` (counted by reason: `prepared`, `closed`, `duplicate`,
`blocked`, `not selected (<outcome>)`, `explicit answers only`, `application not
found`, `same application as another listing`, `over --limit`), `retried`,
`prepared` (now prepared), `holds_before`, `holds_cleared`, `holds_open` (holding the
retried applications now, new ones included), `transitions` (previous outcome ->
outcome -> count) and `ledger_lines_ignored` (of the retried ledger). The Markdown
summary shows them under "Retry of BATCH_ID", including "holds cleared: N of M".

Running the same retry `--batch-id` again continues it like any batch (settled rows
are not launched again). A new `--retry` of the original batch selects again from
where the applications stand then. A job that did not run because another run still
held the application (a timed-out run's claim lapses within five minutes) is
`failed_retryable`, so a later retry picks it up. Exit status: `0` (also when nothing
needed a retry), `1` when the batch has no ledger, `2` for a usage or validation
error, `130` when interrupted.

## Open holds (`interviewmaxxing holds`)

```sh
interviewmaxxing [--home DIR] holds [--candidate ID] [--json]
```

Groups the open holds of every `NEEDS_INPUT` application of the candidate
(`--candidate`, default `IMX_CANDIDATE_ID`) in the state database by their complete
question wording (case, spacing, sentence punctuation and required markers ignored),
so that each distinct question is answered once. For each group: the question (cut to
80 characters), the number of holds and of applications, the backends (the bound
job's ATS), the most frequent reason (and every reason counted), semantic type and
control type, the hold category, a sample application and its field id, and the line
that clears it:

- `interviewmaxxing answer SAMPLE --set FIELD=VALUE --reuse global` (replace `VALUE`;
  a choice takes an option value or label, `interviewmaxxing status SAMPLE` lists
  them). Saved with `--reuse global`, the answer applies to every application that
  asks the same wording. A sample without a semantic type is preferred, because its
  saved answer carries none and so matches the question on every form.
- `interviewmaxxing resume SAMPLE --act` for a sign-in, CAPTCHA, custom control or
  file, which the person completes in a visible browser window.

Holds answered after their application stopped (the rule above) are not listed; the
report counts them (`answered_holds`) and the applications left with nothing open
(`answered`), which wait for `resume` or `prepare-batch --retry`. Applications
prepared for final review (`prepared`) and those stopped without a recorded question
(`unrecorded`, for example a final step with validation errors) are counted but not
listed. The groups are ordered by holds, then applications, then first seen. It is
read-only, creates nothing (without a state database it prints an empty report), and
never prints a stored answer value: saved answers are read only for their question,
scope, type and date. `--json` prints the same report (`HoldsReport`). Lines include
`--home DIR` when it was given.

## Batch report

```sh
interviewmaxxing [--home DIR] batch-report [BATCH_ID ...] [--since DATE] [--top N] [--json]
```

Reads the named batch ledgers (`$IMX_HOME/batches/<BATCH_ID>/ledger.jsonl`), or those
with a line finished at or after `--since DATE` (a UTC date such as `2026-09-24`, or an
ISO date and time; the whole ledger of each such batch is read), or every batch ledger
under `$IMX_HOME/batches/` when neither is given, and prints one report for them.
Batch ids and `--since` cannot be combined. It writes no file and creates nothing, not
even `$IMX_HOME`. It opens the existing state database, the same way `status` does (an
idempotent schema check), only for ledger lines older than `missing_items` or its
field ids, and for the failures of `failed_retryable` rows.

Each listing counts once, as its latest launched entry (any outcome except
`already_recorded`) in the selected ledgers, else its latest `already_recorded`
entry. Latest is by `finished_at`; ties go by batch id order, then line order. A
retry batch's lines carry the original listing ids, so combining a batch with its
retries shows where each listing stands now. The report shows:

- **Totals** by outcome and a **backend × outcome** table for those rows. A row
  without a backend is counted as `(none)`.
- **Backend readiness**, one row per backend: `applications` (listings), `prepared`,
  `needs_input`, `failed` (`failed_retryable` runs that reached a form, and `error`),
  `closed`, `no_form` (`failed_retryable` runs that never reached a fillable form: "Could
  not reach the application form: …"), `other` (`duplicate`, `blocked`,
  `already_recorded`), `prepared_rate` (prepared over every column but `other`, 0..1;
  shown as a percentage), `median_duration_s` (over every launched attempt) and
  `provider_cost_usd` (known cost of these listings, each at its latest line with a
  cost; `null` when none).
- **Durations** per backend: the number of launched attempts and their median and
  p95 in seconds, over every launched attempt in the selected ledgers, plus the
  same over all backends.
- **Provider cost**, when rows carry a cost (see [Files](#files)).
- **Holds by category**: the questions and actions that stopped those rows. For
  each category: the number of holds, the number of applications, the most frequent
  questions (at most `--top N`, 1..100, default 10) and the application ids.
  Categories with the most holds come first; empty ones are left out.
- **Questions**: the same holds grouped by question wording, as `holds` groups them
  (question, holds, applications, backends, reason and every reason counted,
  semantic type, control type, category, sample application, field id and the
  `answer … --reuse global` or `resume … --act` line). Wording is compared after the
  ledger's 120-character cut. A hold whose field id is not known (a ledger line older
  than field ids, read without the state database) gets no line; `status SAMPLE`
  shows it. The Markdown shows the first `--top N` groups; the JSON has all of them.
- **Fill failures**: each `failed_retryable` row's failure, read from its
  application's latest `application.failed_retryable` event recorded by the time the
  row finished. When the event's metadata has `failed_fields`
  (`[{"field_id", "label", "status", "detail"}]`), each failed field counts (kind
  `field`, grouped by status and detail, with up to five field labels); otherwise the
  event's `failure_reason` counts (kind `run`), and without a state database the
  row's own message. `error` rows count by kind of error (`timed out; the job was
  stopped`, `timed out; the job did not exit after SIGTERM and SIGKILL`, `could not
  start the CLI`, `the CLI printed no readable outcome`); their CLI output is never
  shown. Details are masked before grouping and display: URLs become `<url>`, e-mail
  addresses `<email>`, quoted values `'…'`, numbers of four or more digits `#`, the
  runner's provider cost note is dropped, and the text is cut to 120 characters.
  Groups show the number of failures and applications, the backends and a sample
  application; the Markdown shows the first `--top N`, the JSON all.
- **Pipeline cards**, when some row has a card: rows with a card, linked, not
  linked, moved to Closed, not moved, and the reasons counted as in `summary.json`.
  For each listing, its latest entry with a link result counts as linked or not
  linked, and its latest entry with a Closed-move result counts as moved or not.
- The number of unreadable ledger lines, when there are any (`ledger_lines_ignored`).

`--json` prints the same report as JSON (`BatchReport`: `batches`, `batches_dir`,
`since`, `ledger_lines_ignored`, `rows`, `totals`, `by_backend`, `durations`,
`duration_median_s`, `duration_p95_s`, `holds`, `pipeline`, the provider cost fields,
`questions`, `fill_failures` and `backends`). Markdown table cells escape `|`. Hold
categories:

| Category | Rule (checked in this order) |
| --- | --- |
| `custom_control` | Reason `UNSUPPORTED_CONTROL`. |
| `explicit_answer` | Reason `EXPLICIT_ANSWER_REQUIRED` or `UNCOVERED_ATTESTATION`. |
| `lookup` | Reason `NO_ANSWER` on a `TYPEAHEAD` control. |
| `screener_yes_no` | Reason `NO_ANSWER`, and the question starts with the words "do you", "have you" or "are you" (ignoring case, spacing and leading marks such as `*`, `✱` or quotes). |
| `narrative` | Reason `NO_ANSWER`, and the question contains the word "describe", "tell", "why" or "explain". |
| `other` | Anything else, including `AMBIGUOUS`, `USER_ACTION` and an unknown reason. |

A hold's reason, control type, field id and semantic type come from the ledger's
`missing_items`. A ledger line written before that field existed has only
`missing_labels` and `missing_reasons`, and one written before field ids has
`missing_items` without them. For such lines, when the state database exists, each
label is looked up among the application's recorded questions (those of its latest
`NEEDS_INPUT` event, then those of its latest packet), matched by the 120-character
label; a match gives what the line lacks. A label not found there takes the line's
reason when the line has exactly one, and otherwise counts as `other`.

The ledger keeps the full question labels (cut to 120 characters) under
`$IMX_HOME`. The report prints them cut to 80 characters, in text and JSON, and
never prints CLI messages; failure details are masked as described above.

Exit status: `0` on success, including when there are no ledgers (the report then
says `No batch ledgers under <dir>.`), `1` when a named batch has no ledger, and
`2` for an invalid batch id or date, or batch ids with `--since`.

```sh
interviewmaxxing batch-report --top 5                  # every batch
interviewmaxxing batch-report big1 big1-retry-20260924T210507Z --json
interviewmaxxing batch-report --since 2026-09-24
interviewmaxxing status app_example                    # a sample's questions and options
```

## Reviewing prepared applications

```sh
interviewmaxxing status                # every application and its state
interviewmaxxing status APP            # recorded questions, events, next steps
interviewmaxxing events APP            # includes preparation.ready with the review step
interviewmaxxing answer APP --set FIELD=VALUE ...   # then: interviewmaxxing resume APP
interviewmaxxing resume APP --act      # visible window for sign-in, CAPTCHA, custom controls
```

The runner records these events for each application:

- `preparation.ready`: the review step.
- `provider.budget`: calls, known cost, unknown-cost calls, latency by purpose, and the budget
  limits used.
- `routing.trace`: once per resolved step, a projection of each field's route decision and
  of the new decision traces. It holds ids, statuses, scores and page wording, and never
  answer values, fact text, drafts or review text.
- `field.suggestion_chosen`: a lookup label the resolver chose.
- `application.failed_retryable`: after a fill failure its metadata carries `failed_fields`
  (`field_id`, `label`, `status`, and a `detail` with quoted values redacted).

`summary.json` lists the application ids per outcome, so a script or the dashboard
can pick up the `prepared` ones. Evidence lives under `$IMX_HOME/artifacts/APP/`.
Resuming a prepared application re-inspects the site and stops at the same review
step again; submission stays disabled for it.

Verification: `tests/core/test_batch.py` runs the harness offline against a fake
`apply` command, including card links and Closed moves with a store-backed fake;
`tests/core/test_batch_report.py` covers `batch-report` (question groups, fill
failures, backend readiness, several batches and `--since`, the JSON schema);
`tests/core/test_batch_retry.py` covers `--retry` against a store-backed fake of
`apply` and `resume`; `tests/core/test_batch_holds.py` covers `holds`, including
running one generated `answer` line; `tests/core/test_batch_hardening.py` covers the
directory modes, the single state connection, rows sharing a URL, runs stopped
mid-fill, unreadable ledger lines, Closed moves only for closed jobs, lookups outside
the workers' lock, timeout escalation and escaped table cells;
`tests/service/test_application_links_batch.py` checks that the service accepts the
links a batch writes and shows its Closed moves; `e2e/test_batch_e2e.py` runs the
harness with three workers, real headless Chromium and the fictional candidate
against the localhost mock ATS, then the loop (`holds`, a retry that skips explicit-only
holds, one `answer` line per shared question, a retry that prepares them, a combined
report), and checks that the server received no submission.

## Submitting what you approved

`prepare-batch` never submits. Once you have reviewed a prepared application, approve
it with `interviewmaxxing approve APP`; then submit every approved application of the
batch, each exactly as approved:

```sh
IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit-approved --batch BATCH_ID --slots 3 --yes
interviewmaxxing batch-report BATCH_ID   # now with a Submissions table
```

It uses the same worker slots and browser profiles, appends one `kind: "submission"`
line per application (outcome and receipt id) to the batch's ledger, and never
launches an application that line records as submitted or uncertain again. A form
that changed since you approved it is not submitted and comes back as `needs_input`.
`prepare-batch --retry` leaves an approved application alone (skipped as
`approved (left to submit-approved)`): preparing it again would withdraw the
approval. The rules, events and exit codes are in [submission.md](submission.md).
