# Mass preparation

`interviewmaxxing prepare-batch` prepares many Saved jobs from an application URL
inventory. Each job is run through the ordinary `apply` command in its own process,
so the canonical runner, the store claims, the pinned resume, the durable no-submit
restriction and the evidence apply unchanged. The harness adds bounded parallel
workers, a resumable ledger, a readiness summary and links from the Saved pipeline
cards to the resulting applications. It does not add a way to submit.
`interviewmaxxing batch-report` summarizes one or more batch ledgers afterwards,
`interviewmaxxing holds` lists every open question once with the line that answers it
(or, with `--sheet`, writes them all to one answer sheet that `answer --sheet` applies),
and `prepare-batch --retry` runs the held and failed applications of a batch again.

The loop at scale:

```sh
interviewmaxxing prepare-batch --inventory inventory.json --workers 3 --batch-id big1
interviewmaxxing batch-report big1          # outcomes, backends, questions, fill failures
interviewmaxxing holds --sheet sheet.json --batch-id big1  # this batch's open questions, once each
$EDITOR sheet.json                          # set "answer" where you can
interviewmaxxing answer --sheet sheet.json --dry-run  # what would be saved, per entry
interviewmaxxing answer --sheet sheet.json  # applies each answer to every application asking it
interviewmaxxing prepare-batch --retry big1 # the answered and failed ones again, same settings
interviewmaxxing batch-report --family big1 # yield per run; the prepared ones to review
```

For a handful of questions, `interviewmaxxing holds` without `--sheet` prints one
`answer APP --set FIELD=VALUE --reuse global` line per question instead (see
[Open holds](#open-holds-interviewmaxxing-holds)).

## The mass run

The pilots (`pilot7-20260924` and the ones before it) prepared or held a few dozen jobs of
the Saved inventory. The mass run prepares the rest of it without running those again,
then works through its holds one sitting at a time. Every command below is preparation
only: nothing is submitted until you approve an application you reviewed and run its
`submit` line yourself.

```sh
# 0. Where the pilots stand (read-only): the yield of each run, what waits for review.
interviewmaxxing batch-report --family pilot7-20260924

# 1. Prepare the whole Saved inventory on the backends that prepare headless (LinkedIn
#    and the sign-in-gated boards stay out). --exclude-batches leaves out every listing a
#    pilot batch or any of its retries prepared or held (name every pilot batch).
interviewmaxxing prepare-batch --inventory /abs/private/application-urls.json \
  --backends greenhouse,ashby,lever,workable,rippling,jazzhr,bamboohr,breezy,gem \
  --exclude-batches pilot7-20260924 \
  --workers 4 --per-job-timeout 600 --ai-routing --env-file /abs/env.local \
  --writer-model anthropic/claude-opus-5.5 --rag-connection-file /abs/private/connection.json \
  --batch-id mass-20260925
#    Interrupted? Run the same command again (same --batch-id and --exclude-batches).

# 2. Outcomes, backends, questions, fill failures, cost.
interviewmaxxing batch-report --family mass-20260925

# 3. One answer sheet for this batch only (the pilots' old holds stay out of it).
interviewmaxxing holds --sheet /abs/private/mass-sheet.json --batch-id mass-20260925
$EDITOR /abs/private/mass-sheet.json
interviewmaxxing answer --sheet /abs/private/mass-sheet.json --dry-run
interviewmaxxing answer --sheet /abs/private/mass-sheet.json

# 4. Run again what the sheet answered and what failed, with the batch's own settings.
interviewmaxxing prepare-batch --retry mass-20260925 --batch-id mass-20260925-r1

# 5. After a runtime fix that concerns a few applications: only those.
interviewmaxxing prepare-batch --retry mass-20260925 --only-app APP1,APP2 \
  --batch-id mass-20260925-r2

# 6. The yield of every run so far, and the applications at their final review step.
interviewmaxxing batch-report --family mass-20260925
```

Repeat 3 to 6 with a new sheet (`holds --sheet FILE --batch-id mass-20260925 --force`
replaces the old file) while the yield table still gains prepared applications. A plain
retry skips the applications held only on browser actions (`browser actions only`: a
sign-in, a CAPTCHA, a custom control, a file); clear each with `interviewmaxxing resume
APP --act` in a visible browser (the sheet's `actions` list the lines), or add
`--user-actions` to run them headless again after a runtime fix. Always
retry the first batch (`--retry mass-20260925`), not a retry: its ledger lists every
application of the run, and `answer --sheet` prints exactly that `--retry` line. The
report's "At the final review step" section lists each prepared application with its
`approve` and `submit` lines; review each one first (`interviewmaxxing status APP` and
the evidence under `$IMX_HOME/artifacts/APP/`). `submit` needs `IMX_ALLOW_SUBMISSION=1`
and `--yes`, and submits exactly what you approved
([Submitting what you approved](#submitting-what-you-approved)).

## What it does and does not do

- **Never submits.** Every job runs with the preparation-only runner. A complete
  form stops at the final review step as `NEEDS_INPUT` with a `preparation.ready`
  event (outcome `prepared`). There is no flag in `prepare-batch` or `apply` that
  enables submission; the store also rejects a submission attempt for these
  applications, whatever runs them later.
- **One Chromium per worker slot.** Worker `N` runs with
  `IMX_BROWSER_DIR=$IMX_HOME/browser-workers/wN`, because the runner holds one OS
  lock per browser profile. `submit-approved` slots use `browser-workers/sN` instead,
  so a submission run beside a running batch never shares, or waits on, a prepare
  worker's profile. All workers share the same `IMX_HOME`, state database, candidate
  profile, artifacts directory and jobs store (`IMX_JOBS_DB`), each passed explicitly,
  so per-path overrides are kept. A sign-in stored in one worker profile is not visible
  to the others or to the default `$IMX_HOME/browser` profile.
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
- **Cards are never created, and preparing never advances one.** The batch creates no
  card and no pipeline database; Closed is the only lane preparing moves a card to. A
  submission the site confirmed moves its card to Applied (`submit`, `submit-approved`;
  see [Pipeline cards](#pipeline-cards)).
- **Nothing private is printed.** Progress lines show outcome, backend, company,
  title, duration, application id and, for a row with a card, `[card linked]`,
  `[card not linked]` or `[card linked, moved to Closed]`. Questions and CLI
  messages are only written to the ledger under `IMX_HOME`; a closed job's reason
  also goes into its card's history note in the local pipeline database.

Preparation runs the site's intermediate steps: a multi-step form may store a
draft on the employer's side before the final step, as with a single `apply`.

## The Wellfound lane (sign-in-gated, one worker)

Wellfound applications run in the person's own signed-in Chrome through the OpenCLI Browser
Bridge profile, one worker, prepare-only. The person's account was verified signed in on
September 25, 2026 (candidate navigation, active Apply controls). Nothing is submitted: the
application modal's "Send application" is the dialog's submit and is never clicked.

```sh
interviewmaxxing prepare-batch --inventory /abs/private/application-urls.json \
  --backends wellfound --browser opencli --opencli-profile PROFILE --workers 1 \
  --per-job-timeout 600 --limit 30 --batch-id wellfound-20260925 \
  --captcha-solver 2captcha --captcha-budget-usd 2.00 --ai-routing --env-file /abs/env.local \
  --writer-model anthropic/claude-opus-5.5 --rag-connection-file /abs/private/connection.json
```

Two things observed live and read-only on one posting are being fixed in WP1 round 15: under
OpenCLI the "Apply" control needs a DOM click (its coordinate click and Enter do nothing), and
the application is one note textarea to a named recruiter plus "Send application", a dialog
shape the form rule has to accept. Until that round lands, this lane stops on every job with
"Could not reach the application form".

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

### Where a batch job's location comes from

The inventory has no location column. `prepare-batch` looks each row's listing up by
`listing_id` in the jobs store: `$IMX_JOBS_DB`, else `$IMX_HOME/jobs/jobs.sqlite3`. The
store's `listings` table has every saved listing's JSON, and a listing id replaced by a
merge resolves to the surviving one. The lookup opens one read-only connection and
never creates or changes the store. The listing's `location` ("Austin, TX", "Austin,
Texas Metropolitan Area", "Remote", …) goes to the run as `apply --job-location TEXT`,
and so does its `title` and `company` where the row has none. The row's own `title` and
`company` go as `--job-title` and `--job-company`. The start line says how many rows
got a location ("N with their saved listing's location"). A row without a listing, or
whose listing states no location, runs as before.

The runner writes these on the application's job right after recording the request
(`ApplicationStore.record_listing`), before anything reads the job:

- A **listing location** fills a job without one. It also replaces a location a page
  gave (a JSON-LD locality bound with the job's identity, or one bound before sources
  were recorded). The first listing location is kept.
- A **page's locality** is written only when the job has no location. A remote posting
  whose JSON-LD names the company's Austin office therefore stays remote, and an Austin
  hybrid posting keeps "Austin, TX (Hybrid)" over the page's bare "Austin". The metro
  rule reads the job's location and title (Austin metro: on-site or hybrid; elsewhere:
  Remote), so this is what makes it answer Austin jobs correctly.
- **Title and company** from the listing only fill a job without them; the page's own
  still win when the job's identity is bound.

Each location write is recorded on the application as `job.location_bound`: `job_id`,
`location`, `source` (`listing` or `page`) and `previous`. When a job is merged into
another with the same ATS identity, its listing location replaces the other job's page
locality. The ledger line keeps the location the run was given (`location`).
`--retry` passes it again, and a line written before this field existed takes its
listing's location from the jobs store. The service's application handoff writes the
linked card's `locationCommute` (else its listing's location) the same way.

### The listing's description is the job's evidence

A narrative question (why this company, how you use AI, a cover letter) is written from the
job's description, which the writer reads from the knowledge store under the application's
job. The browser reads no description from the page, and an apply page such as Ashby's
`/application` shows none, so with nothing indexed the writer holds the question
(`NO_ANSWER`) before any provider call.

`prepare-batch` therefore passes each row's listing id as `apply --job-listing-id ID`, and
`--retry` passes it as `resume --job-listing-id ID`, except for a row keyed by its URL
(`url:…`). `apply` and `resume` read that listing from the same jobs store (each worker
gets `IMX_JOBS_DB`) and use its description only when its completeness is `FULL`. Before
the run resolves any field, the runner indexes it through the knowledge store's `index_job`
(the call `scripts/rag_answers.py index-job` and `scripts/index_saved_jobs.py` make) under
the job the writer retrieves for (after any identity binding), with the listing's source
URL (else its posting URL) as the source, once per job per run. This needs `--ai-routing
--rag-connection-file`: without a knowledge store nothing is indexed. An unchanged
description needs no new embeddings, and the embedding call counts in the run's provider
budget (`knowledge_embedding`). The knowledge store keeps one current description per job,
so this replaces one indexed for the same job from another source.

Each attempt is recorded on the application as `evidence.job_description`: `source`
(`listing`), `listing_id`, `status` (`indexed`, `unchanged`, or `failed` with the error's
type) and `chunks`; never the description. A failed index does not stop the run: its
narrative questions hold as they would without the listing.

## Command

```sh
interviewmaxxing [--home DIR] prepare-batch --inventory inventory.json \
  --backends greenhouse,lever --limit 40 --workers 3 --max-prepared 20 \
  [--statuses resolved] [--retry-retryable 1] [--per-job-timeout 900] \
  [--batch-id ID] [--include-existing] [--sync-closed | --no-sync-closed] \
  [--exclude-batches A,B] [--candidate ID] [--json]

interviewmaxxing [--home DIR] prepare-batch --retry BATCH_ID \
  [--outcomes needs_input,failed_retryable,unknown,error] [--all] [--include-explicit] \
  [--user-actions] [--only-app APP[,APP]] [--backends A,B] [--limit N] [--max-prepared N] \
  [--batch-id ID] [--workers N] [--per-job-timeout S] [runtime flags] [--json]
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
| `--exclude-batches A,B` | Repeatable. Leaves out every inventory row whose listing earlier batches prepared or held: each named batch counts with its whole retry family (the batch it retried and every retry, `batch-report --family`), and a listing is left out when one of their ledger lines says `prepared` or `needs_input` (or `already_recorded` for a `NEEDS_INPUT` application), matched by listing id or by normalized URL. Listings those batches only saw fail, error, close or duplicate are not left out (a closed or duplicate one is recorded as `already_recorded` as usual; a failed one runs again). Left-out rows are not written to the ledger; `summary.json` counts them as `skipped_excluded` and names the batches in `excluded_batches`. `--limit` counts the rows that are left. A batch id without a ledger exits `1`. Give the same flag again when continuing the batch. |
| `--sync-closed`, `--no-sync-closed` | On by default. When a `closed` row (or an `already_recorded` row whose stored state is `FAILED_PERMANENT`) saw the job closed, that is its reason is the runner's "The job is no longer accepting applications…", and it has a linked card still in Saved, moves the card to Closed through the revision-aware `move_item` with the note `Observed closed on YYYY-MM-DD (UTC) by prepare-batch <batch id>: <observed reason> (application <id>)`. The note appears in the card's history ("Moved from Saved to Closed. …"). No other card field changes. A `FAILED_PERMANENT` for any other reason moves nothing. A card already in Closed is left as it is, with nothing recorded; a card in any other lane is left where it is. `--no-sync-closed` still links cards but moves none. |
| `--json` | Print the summary as JSON (progress lines then go to stderr). |

The runtime flags of `apply` are accepted and passed to every job unchanged:
`--browser`, `--opencli-profile`, `--ai-routing`, `--env-file`, `--writer-model` and
`--rag-connection-file` (see [dynamic-runtime.md](dynamic-runtime.md)). The batch
uses the candidate from `--candidate` or `IMX_CANDIDATE_ID`. `--captcha-solver` and
`--captcha-budget-usd` are passed on too, with one spend cap for the whole batch; see
[CAPTCHAs](#captchas).

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

### A confirmed submission moves the card to Applied

When `submit-approved` records a submission as `submitted` with a receipt, every card
linked to that application (`application_id`) that is still in Saved moves to Applied (id
`applied`, or labelled Applied) through the revision-aware `move_item`, retried after a
concurrent edit in the same way, with a note such as:

```text
Submitted on 2026-09-25 (UTC) by submit-approved mass-20260925-a; the site confirmed it (application app_example, receipt sub_example, confirmation BWA-000777)
```

A single `submit APP --yes` does the same (`by submit`) and prints "Pipeline card moved
from Saved to Applied."; the `submit` processes that `submit-approved` runs leave the move
to the batch (`IMX_SUBMIT_CARDS_BY_BATCH=1`), which records it. `uncertain`, `blocked`,
`needs_input` and `error` never move a card, and a card already in Applied or Closed is left
alone. The submission's ledger line records `applied_synced` (true when a card moved, false
when one could not be, null when there was nothing to move) and `applied_sync_reason`:
`board has no Applied lane`, `card not in Saved (in <lane id>)` (for example a card you
already moved on to `interviewing`; it stays there), `card kept changing` or `move error:
<type>`. The Markdown summary counts them ("pipeline cards moved to Applied: N; not moved:
…") and the progress line ends with `[card moved to Applied]` or `[card not moved to
Applied]`.

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
  - `location`: the saved listing's location the run was given (`--job-location`; see
    [Where a batch job's location comes from](#where-a-batch-jobs-location-comes-from)),
    passed again by `--retry`; empty when the row had none.
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
  normalized URL, or in a retry the same application), `skipped_excluded` and
  `excluded_batches` (`--exclude-batches`: rows left out, and the batch families named,
  each with every retry), `ledger_lines_ignored`,
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
  [--outcomes needs_input,failed_retryable,unknown,error] [--all] [--include-explicit] \
  [--user-actions] [--only-app APP[,APP]] [--backends A,B] [--limit N] [--max-prepared N] \
  [--batch-id ID] [--json]
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
| `needs_input` | NEEDS_INPUT for questions, sign-in, CAPTCHA or a custom control | with `--outcomes` (default), once one of its holds was answered since it stopped; held only on browser actions, with `--user-actions`; the ones `--only-app` names; every one with `--all` |
| `failed_retryable` | FAILED_RETRYABLE | with `--outcomes` (default) |
| `unknown` | REQUESTED, INSPECTING, PACKET_READY or FILLING: a run started and recorded no outcome (it timed out or crashed) | with `--outcomes` (default) |
| `error` | the job never recorded an application (for example the CLI could not start) | with `--outcomes` (default); it runs `apply URL` |

A listing whose ledger line says `prepared`, `closed`, `duplicate` or `blocked` is
never retried either, whatever the store says now. A `needs_input` application runs
again only when something changed for it: at least one of its holds was answered since
it stopped. Running it with nothing answered would stop it at the same questions, so
by default it is skipped (`nothing answered since the stop`); after a runtime fix that
may clear holds, `--all` runs every held one. A held application whose open holds are all
browser actions (a sign-in, a CAPTCHA, a custom control or a file: nothing `answer` can
answer, so they never count as answered) is skipped as `browser actions only`: a headless
retry meets the same page again, and hitting a site that challenged the run again only
makes more challenges likely. Clear each with `interviewmaxxing resume APP --act` in a
visible browser (`holds` lists the line), or run them headless again with `--user-actions`
(for example after a transient challenge or a runtime fix for a control); the summary says
so under "Retry of". `failed_retryable`, `unknown` and `error` applications always run
again. A `needs_input` application whose open holds are all
`EXPLICIT_ANSWER_REQUIRED` is skipped unless `--include-explicit` (also with `--all`):
only the person can answer those. A hold is no longer open once, after the
application stopped, the person answered exactly that question for it
(`interviewmaxxing answer APP`, or `answer --sheet`, which saves each entry on each of
its applications), or saved an answer whose question is the same wording (compared as
the resolver compares it), whose semantic type is unset or the hold's, and which
applies to the job (`--reuse global`, or `--reuse job` for this job). So after
`answer --sheet`, a plain `--retry` (no `--all`) runs every application that received
an answer, and every other application whose question a `global` entry answered (also
one of another batch, or one taken out of the entry's `fields`). An answer that existed
before the stop was already available to the run that stopped, so it does not count. A
second listing of an application already selected (an alias URL) is skipped.
`--backends` keeps the listings of those backends; `--limit N` runs at most N of the
selected ones.

`--only-app APP[,APP]` (repeatable) keeps only those applications of the ledger (a
listing counts when its line, or the store's application for its URL, is one of them)
and runs each held one even with nothing answered since it stopped: naming it is the
reason, so a handful can run again after a fix without `--all` (one held only on browser
actions runs too, without `--user-actions`). The other rules stand: prepared, closed and
approved applications are still skipped, explicit-only holds still need
`--include-explicit`, and `--outcomes`, `--backends` and `--limit` still apply. An id
that is none of the ledger's applications is a usage error (exit `2`) naming it.

The selected applications run as a new batch (`--batch-id`, default
`BATCH_ID-retry-YYYYmmddTHHMMSSZ`; it must differ from `BATCH_ID`) with the same
harness: one `interviewmaxxing resume APP --json --headless` subprocess per
application (`apply URL` for an `error` row without one), per-slot browser profiles,
the per-job timeout, the card bookkeeping, and nothing submitted: `resume` keeps each
application's preparation-only restriction. The worker count, per-job timeout, retry
count, closed-card sync, candidate, browser and runtime flags (`--ai-routing`,
`--env-file`, `--writer-model`, `--rag-connection-file`, `--opencli-profile`,
`--captcha-solver`, `--captcha-budget-usd`) come from
the retried batch's `summary.json` (`run_options`); a flag given with `--retry`
replaces its recorded value, even when it is given with its default value. A batch
whose summary predates `run_options` uses the flags given (a note says so).
`--max-prepared` is the retry's own; `--inventory`, `--statuses` and
`--include-existing` do not apply. The retried batch's ledger is only read.

The retry's ledger lines carry `retry_of`, `previous_outcome`, `holds_before` and
`holds_cleared`, and its `summary.json` a `retry` object: `retry_of`, the `outcomes`
selected, `include_explicit`, `rerun_all` (`--all`), `user_actions` (`--user-actions`),
`only_apps` (`--only-app`), `considered` (listings after `--backends` and `--only-app`),
`selected`, `selected_by` (why, each selected listing counted under the first reason
that applies: its outcome `failed_retryable`, `unknown` or `error`, or, held,
`answered since the stop`, `--only-app` (named, nothing answered), `user actions` (only
browser actions open, `--user-actions`) or `--all`), `skipped` (counted by reason:
`prepared`, `closed`, `duplicate`, `blocked`, `approved (left to submit-approved)`, `not
selected (<outcome>)`, `nothing answered since the stop`, `browser actions only`,
`explicit answers only`, `application not found`, `same application as another
listing`, `over --limit`), `retried`, `prepared` (now prepared), `holds_before`,
`holds_cleared`, `holds_open` (holding the retried applications now, new ones
included), `transitions` (previous outcome -> outcome -> count) and
`ledger_lines_ignored` (of the retried ledger). The header line and the Markdown summary
show them under "Retry of BATCH_ID", including "selected by: answered since the stop
(N)" and "holds cleared: N of M".

Running the same retry `--batch-id` again continues it like any batch (settled rows
are not launched again). A new `--retry` of the original batch selects again from
where the applications stand then. A job that did not run because another run still
held the application (a timed-out run's claim lapses within five minutes) is
`failed_retryable`, so a later retry picks it up. Exit status: `0` (also when nothing
needed a retry), `1` when the batch has no ledger, `2` for a usage or validation
error, `130` when interrupted.

## Open holds (`interviewmaxxing holds`)

```sh
interviewmaxxing [--home DIR] holds [--candidate ID] [--batch-id ID ... | --since DATE] [--json]
```

Groups the open holds of every `NEEDS_INPUT` application of the candidate
(`--candidate`, default `IMX_CANDIDATE_ID`) in the state database by their complete
question wording (case, spacing, sentence punctuation and required markers ignored),
so that each distinct question is answered once.

`--batch-id ID` (repeatable) reads only the applications those batches hold or failed:
each listing of the batch at its latest line there, unless that line says `prepared`,
`closed`, `duplicate` or `blocked` (the applications `prepare-batch --retry ID`
considers), resolved as the retry resolves them (the line's application, else the
store's application for the listing's URL, for this candidate). An application that
failed in the batch and is held now (after a retry) is included; one the batch prepared
is not, whatever the store says now, because a retry of the batch would not run it.
`--since DATE` does the same for the batches with a ledger line finished at or after
DATE, as `batch-report --since` selects them. The two cannot be combined; a batch id
without a ledger exits `1`. The report then names the batches (`batches`, `since` in
the JSON; "only the applications held or failed in: …" in the Markdown), and its
counts cover those applications only.

For each group: the question (cut to 80 characters), the number of holds and of
applications, the backends (the bound job's ATS), the most frequent reason (and every
reason counted), semantic type and control type, the hold category, a sample
application and its field id, and the line that clears it:

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
scope, type and date. `--json` prints the same report (`HoldsReport`; it names no
database path). Lines include `--home DIR` when it was given.

## One sitting: the answer sheet

After a large batch most open questions are legitimately personal or one-off (the AI
tools you used, a favourite restaurant, how familiar you are with the company, whether
you are comfortable leading client conversations, a government official in the family,
a non-compete you signed). One `answer` line per question, with a `status APP` for each
to see its options, does not scale to a hundred of them. The answer sheet puts every
distinct open question, with its options, into one private file that you fill in once:

```sh
interviewmaxxing [--home DIR] holds [--candidate ID] --sheet FILE [--batch-id ID ... | --since DATE] [--force]
$EDITOR FILE
interviewmaxxing [--home DIR] answer --sheet FILE --dry-run
interviewmaxxing [--home DIR] answer --sheet FILE [--batch-id ID]
interviewmaxxing prepare-batch --retry BATCH_ID
```

`holds --sheet FILE` groups the open holds exactly as `holds` does (same candidate, same
wording key, holds answered since their stop left out, and with `--batch-id` or `--since`
the same applications only: those the batches hold or failed) and writes them as JSON,
owner-only (`0600`), refusing to replace an existing file unless `--force` (a sheet may
hold answers you typed). After a batch, `--batch-id BATCH` keeps the sheet to that batch's
questions instead of every held application's (older pilots included). It prints counts
only: how many questions, how many with a proposal, how many browser actions and held
applications (and of which batches), and the `answer --sheet` line to run next. The file
has:

- `questions`: one entry per distinct question, most applications first:
  - `question`: the complete wording as first recorded (not cut).
  - `semantic_type` (the most frequent known type, or null), `control_type`, `reason`
    and `backends`, as in `holds`.
  - `options`: the usable options as recorded on the first application (`value` and
    `label`, placeholders and disabled options left out). When another application
    recorded different options for the same wording (referral sources differ by
    employer), `options_by_application` lists that application's options.
  - `applications` and `fields`: how many applications ask it, and the field id of the
    question on each (`{"app_...": "field_id"}`).
  - `reuse`: `"global"` by default; change it to `"job"` (saved for each application's
    job) or `"application"` (these applications only, nothing saved for reuse).
  - `answer`: `null`. Put your answer here: text; an option's value or label (case and
    spacing ignored); several separated by `;` (or a JSON list) for a multi-select or
    checkbox group; `yes` or `no` (or `true`/`false`) for a checkbox. A required
    checkbox is only accepted checked, as with `answer`.
  - `proposal` and `proposal_basis`, only when one of the applications' recorded routing
    traces (`routing.trace`, the projection `events APP --verbose` shows) names a
    candidate the resolver found but did not place because it scored below its gate: a
    saved answer whose reworded wording scored below the gate (`question_equivalence`;
    the proposal is that saved answer's value, `reference_ids` its ids), a work
    authorization or sponsorship option derived from the stated status below the gate
    (`status_derivation`), an exact saved answer mapped onto an option below the gate
    (`option_equivalence`), a fact screener's best option (`fact_screener`) or an
    undecided yes/no screener leaning one way (`experience_screener`). The basis gives
    the `stage`, the decision's `score` (its probability) and `confidence`, the trace
    `status`, the `application_id` whose trace it was and `"confirmed": false`. A
    proposal is exactly that: nothing applies it unless you copy it into `answer`. The
    latest trace of each stage counts, stages in the order above, the person's own saved
    answers before anything derived from facts.
- `actions`: the holds that need the browser (sign-in, CAPTCHA, an unsupported control,
  a file), each with its wording, reason, `fields` keyed by application like the
  questions' (the field id, or `null` for a page-level action such as a sign-in or a
  CAPTCHA), so that a filter by application keeps them, `application_ids` and one
  `resume APP --act` line per application.
- `batches` and `since`: the batches the sheet was limited to (`--batch-id`, or those
  `--since` selected); empty for a sheet of every held application.
- `held` and `open_holds`: the counts behind the entries; `candidate_id`, `generated_at`,
  `version` and a `note` recalling the rules.

`answer --sheet FILE` (no `APPLICATION_ID`, `--set` or `--answers`) reads the sheet back
and applies every entry whose `answer` is not null to each application in its `fields`,
through the same path as `answer APP --set FIELD=VALUE --reuse SCOPE`: the value is
checked against the question as recorded on that application (its options, its control),
saved as the application's own user input, and, for `global` or `job`, saved for reuse
with that scope. A hold answered since its application stopped (by `answer`, by an earlier
sheet, or by a saved answer for its wording) is left as it is, so applying the same sheet
twice changes nothing the second time, and a sheet generated after the answers were saved
no longer lists them. An application stopped again since the sheet was written (a retry)
is checked against its new stop: when the options it recorded for the question now
differ from those the sheet shows for it (`options`, or its `options_by_application`;
compared by label, case, spacing and order ignored), the entry is skipped there
(`options changed`), since the answer was chosen among the options you saw; a lookup's
options are the site's suggestions for what was typed and never count as changed. When
its field id now asks another question, it is skipped as `not open`. Regenerate the
sheet for those.

It prints counts and wordings, never a value or an option: entries answered, answers
saved and applications concerned, holds already answered, then one line per answered
entry, in sheet order:

```text
- 2 of 3 application(s) received it; options changed on 1: Which of these tools have you used?
- 0 of 1 application(s) received it; options changed on 0; invalid on 1: Before applying, how familiar were you with this company?
```

that is how many of the entry's applications received the answer, how many were
skipped because their recorded options changed since the sheet was written, and any
other reason with its count (`invalid`: the answer is not one of that application's
recorded options or has the wrong shape, so that entry is skipped there and the rest
are applied; `already answered`; `not open`: the field is not a recorded question of the
application's current stop, or asks another question now; `not waiting`: the
application is no longer NEEDS_INPUT; `not found`; `busy`: another run holds it).

`--dry-run` runs every one of those checks and prints the same lines ("would save",
"would receive it") without saving anything: no user input, no saved answer, no event,
no claim (a claim another run holds is read, and counted as `busy`). Its last line is the
`answer --sheet FILE` that saves them. Without `--dry-run` the last lines are the
`prepare-batch --retry BATCH_ID` to run next: `--batch-id` when given; else, for a sheet
limited to batches, the first batch of each one's retry family (the batch it retried,
whose ledger lists every application of the run); else the first batch of the family of
the batch under `$IMX_HOME/batches` whose ledger was written last; else a `resume APP`
hint. Exit status: `0` when the sheet was applied or checked (also when nothing was left
to apply), `1` without a state database, `2` for a sheet that does not validate (the
message names the entry and field, never a value), for a `--sheet` combined with an
application id, or for `--dry-run` without `--sheet`.

The sheet is the one place your typed values live outside the profile and the state
database: keep it out of source control and delete it once applied. Reusable keys
([simple-answers.md](simple-answers.md)) stay the first choice for questions that recur
across employers; the sheet is for the long tail a batch surfaces.

## Batch report

```sh
interviewmaxxing [--home DIR] batch-report [BATCH_ID ...] [--family BATCH_ID ...] [--since DATE] \
  [--top N] [--json]
```

Reads the named batch ledgers (`$IMX_HOME/batches/<BATCH_ID>/ledger.jsonl`) and, for
each `--family BATCH_ID` (repeatable), the ledgers of that batch's whole retry family:
the batch it retried (followed back to the first batch) and every `prepare-batch --retry`
of it or of its retries (a retry's `summary.json`, or its ledger lines, name the batch it
retried). Or those with a line finished at or after `--since DATE` (a UTC date such as
`2026-09-24`, or an ISO date and time; the whole ledger of each such batch is read), or
every batch ledger under `$IMX_HOME/batches/` when none of these is given, and prints one
report for them. Batch ids or `--family`, and `--since`, cannot be combined. It writes no
file, creates nothing, not even `$IMX_HOME`, and submits nothing. It opens the existing
state database, the same way `status` does (an idempotent schema check), for ledger lines
older than `missing_items` or its field ids, for the failures of `failed_retryable` rows,
and to tell which applications wait at their final review step.

Each listing counts once, as its latest launched entry (any outcome except
`already_recorded`) in the selected ledgers, else its latest `already_recorded`
entry. Latest is by `finished_at`; ties go by batch id order, then line order. A
retry batch's lines carry the original listing ids, so combining a batch with its
retries shows where each listing stands now. The report shows:

- **Totals** by outcome and a **backend × outcome** table for those rows. A row
  without a backend is counted as `(none)`.
- **Yield over retries**, for each batch read together with at least one retry of it
  (and for every `--family`, even alone): one row per run of the family (one batch id),
  in the order they started, the first batch first:

  ```text
  | # | batch | finished (UTC) | ran | prepared | prepared total | held | failed | holds open | cost USD |
  | 1 | big1 | 2026-09-24 18:00 | 20 | 5 | 5 of 20 | 12 | 3 | 40 | 4.0000 |
  | 2 | big1-retry-20260924T210507Z | 2026-09-24 21:20 | 15 | 4 | 9 of 20 | 9 | 2 | 25 | 2.5000 |
  ```

  `ran` is the listings the batch launched and `prepared` those it prepared; the other
  counts are where the family's listings stood once it finished, each at its latest line
  of this or an earlier run (as the rows below count them): `prepared total` (of the
  family's listings), `held` (`needs_input`), `failed` (`failed_retryable` or `error`) and
  `holds open` (questions and actions on the held listings' latest lines). `cost USD` is
  the run's own known provider cost: the family's known cost after it minus before it (a
  line's cost is its application's total so far, so a run of these applications outside
  the family in between counts in the next run). The family's total follows the table.
  Retries read without the batch they retried get no table. The JSON has `families`
  (`root`, `batches`, `listings`, `runs`: `batch_id`, `retry_of`, `started_at`,
  `finished_at`, `ran`, `prepared`, `prepared_total`, `held`, `failed`, `holds_open`,
  `cost_usd`, `cost_total_usd`) and `requested_families`.
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
  addresses `<email>`, quoted values `'…'`, every group of three or more digits `#` (an
  unquoted phone number such as `+1 512-555-0142 (+1)` shows as `+1 #-#-# (+1)`), the
  runner's provider cost note is dropped, and the text is cut to 120 characters.
  Groups show the number of failures and applications, the backends and a sample
  application; the Markdown shows the first `--top N`, the JSON all.
- **Pipeline cards**, when some row has a card: rows with a card, linked, not
  linked, moved to Closed, not moved, and the reasons counted as in `summary.json`.
  For each listing, its latest entry with a link result counts as linked or not
  linked, and its latest entry with a Closed-move result counts as moved or not.
- The number of unreadable ledger lines, when there are any (`ledger_lines_ignored`:
  lines that are not JSON objects, or prepare lines that do not validate), and of
  unreadable submission lines (`submissions.lines_ignored`: lines marked
  `kind: "submission"` that do not validate). Each unreadable line counts once.
- **At the final review step**, last: every row's application stopped at its final
  review step now (by the state database: `NEEDS_INPUT` right after a
  `preparation.ready`, whatever the row's line says; without a state database, the rows
  whose line says `prepared`), each application once, with the lines you run after
  reviewing it:

  ```text
  | application | job | backend | approve | submit |
  | app_example | Example Co — Growth Marketing Manager | greenhouse | `interviewmaxxing approve app_example` | `IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit app_example --yes` |
  ```

  An application you already approved shows `approved` instead of its `approve` line;
  one whose review step shows a CAPTCHA gets `--act` on its `submit` line (you solve it in
  a visible window). The report runs none of these lines and submits nothing; review
  each application first (`interviewmaxxing status APP` and the evidence under
  `$IMX_HOME/artifacts/APP/`). `submit` refuses an application you did not approve and
  needs `IMX_ALLOW_SUBMISSION=1` and `--yes`. The JSON has `ready`: `application_id`,
  `listing_id`, `batch_id`, `company`, `title`, `backend`, `approved` (`null` without a
  state database), `captcha_pending`, `review`, `approve` (`null` once approved) and
  `submit`.

`--json` prints the same report as JSON (`BatchReport`: `batches`, `batches_dir`,
`since`, `requested_families`, `ledger_lines_ignored`, `rows`, `totals`, `by_backend`,
`durations`, `duration_median_s`, `duration_p95_s`, `holds`, `pipeline`, the provider
cost fields, `questions`, `fill_failures`, `backends`, `submissions`, `families` and
`ready`). Markdown table cells escape `|`. Hold categories:

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
says `No batch ledgers under <dir>.`), `1` when a named batch (or `--family` batch) has
no ledger, and `2` for an invalid batch id or date, or batch ids or `--family` with
`--since`.

```sh
interviewmaxxing batch-report --top 5                  # every batch
interviewmaxxing batch-report big1 big1-retry-20260924T210507Z --json
interviewmaxxing batch-report --family big1            # big1 and every retry of it
interviewmaxxing batch-report --since 2026-09-24
interviewmaxxing status app_example                    # a sample's questions and options
```

## Reviewing prepared applications

```sh
interviewmaxxing status                # every application and its state
interviewmaxxing status APP            # recorded questions, events, next steps
interviewmaxxing events APP            # includes preparation.ready with the review step
interviewmaxxing events APP --verbose  # also the prompts, candidates and traces
interviewmaxxing answer APP --set FIELD=VALUE ...   # then: interviewmaxxing resume APP
interviewmaxxing resume APP --act      # visible window for sign-in, CAPTCHA, custom controls
```

`events` prints every form URL as a page address (scheme, host and path: sites put
per-session draft tokens in the query and fragment, which the store keeps exactly).
Recorded questions' prompts (a lookup's prompt quotes the typed value and the site's
suggestions), ambiguous answers' candidates, a lookup's suggestions, a chosen lookup
label and routing traces can carry your own values; they show as
`(hidden; --verbose shows it)` unless `--verbose` is given. `status APP --json` prints
the form URLs of its packet, approval, attempts and recorded questions the same way (the
job's and requests' application URLs are yours and stay as given), and `status` never
prints the state database's path (`interviewmaxxing paths` does).

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
`apply` command, including card links and Closed moves with a store-backed fake, batch
families (`BatchTree`) and `--exclude-batches` (listings a batch or its retry prepared or
held left out by listing id or URL, the others run, `--limit` after the exclusion, the
usage errors); `tests/core/test_batch_report.py` covers `batch-report` (question groups,
fill failures, backend readiness, several batches and `--since`, the JSON schema, the
yield over retries of a family and `--family`, and the applications at their final
review step with their `approve` and `submit` lines, approved ones, a pending CAPTCHA,
ones that moved on, and nothing submitted); `tests/core/test_batch_retry.py` covers
`--retry` against a store-backed fake of `apply` and `resume`, the sheet's answers
counting as answered for a default retry (also through a global answer's wording, and
in another batch) and `--only-app`; `tests/core/test_batch_holds.py` covers `holds`,
including running one generated `answer` line; `tests/core/test_answer_sheet.py` covers
`holds --sheet` and `answer --sheet` (the entries and actions with their `fields`, the
file mode, proposals from below-gate traces and that they are never applied, validation
failures reported by wording, idempotence, a sheet and `holds` limited to batches or
`--since`, options that changed since the sheet was written, a field that asks another
question, per-entry counts, `--dry-run` saving nothing, and the retry lines);
`tests/core/test_batch_privacy.py` covers the masked failure details, what `events`,
`status --json` and `holds --json` print, and the unreadable submission lines;
`tests/core/test_batch_hardening.py` covers the directory modes, the single state
connection, rows sharing a URL, runs stopped mid-fill, unreadable ledger lines, Closed
moves only for closed jobs, lookups outside the workers' lock, timeout escalation and
escaped table cells; `tests/service/test_application_links_batch.py` checks that the
service accepts the links a batch writes and shows its Closed moves;
`e2e/test_batch_e2e.py` runs the harness with three workers, real headless Chromium and
the fictional candidate against the localhost mock ATS, then the loop (`holds`, a
default retry that runs nothing before any answer, an `--all` retry that skips
explicit-only holds, one shared question answered with its `answer` line and the other
through `holds --sheet --batch-id`, `answer --sheet --dry-run` and `answer --sheet`, a
default retry that runs all three and prepares two, an `--only-app` retry, the family's
yield and review lines from `batch-report --family`, and a run over a new inventory
that leaves the batch's listings out with `--exclude-batches`), and checks that the
server received no submission.

## Submitting what you approved

`prepare-batch` never submits. Once you have reviewed a prepared application, approve
it with `interviewmaxxing approve APP`; then submit every approved application of the
batch, each exactly as approved:

```sh
IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit-approved --batch BATCH_ID --slots 3 --yes
interviewmaxxing batch-report BATCH_ID   # now with a Submissions table
```

Its slots have their own browser profiles (`$IMX_HOME/browser-workers/s<slot>`, never a
prepare worker's `w<slot>`), so it runs beside a prepare batch without either waiting on
the other's profile lock. It appends one `kind: "submission"` line per application
(outcome and receipt id) to the batch's ledger, and never launches an application that line
records as submitted or uncertain again. A submission that did not run (`blocked`: another
run was using its profile or held the application) keeps its approval, so running the same
command again submits it, once. A confirmed submission moves the application's Saved card
to Applied ([above](#a-confirmed-submission-moves-the-card-to-applied)). A form
that changed since you approved it is not submitted and comes back as `needs_input`, and so
does one whose site resumed a draft it kept at a later page; those are listed on their own
(`kept_drafts`) with the remedy, submitting them in the browser yourself, because preparing
them again reopens the same draft.
`prepare-batch --retry` leaves an approved application alone (skipped as
`approved (left to submit-approved)`): preparing it again would withdraw the
approval. A submission line that cannot be read (cut short by a crash, edited) is
counted as `ledger_lines_ignored` in `submission-summary.json` and its Markdown, and in
`batch-report` (`submissions.lines_ignored`): its application counts as not submitted by
that ledger, and the store still refuses a second submit. The batch's own ledger holds its
prepare lines too: a line cut short before the first submission line was a prepare line
(counted by prepare-batch's reader, not as a submission), a submission line is never
appended onto such a line, and a cut line after a submission line counts as possibly a
submission. The rules, events and exit
codes are in [submission.md](submission.md).

## CAPTCHAs

A CAPTCHA stops an application for you ("Solve the CAPTCHA", `needs_input`) unless the run
solves it through your 2Captcha account:

```sh
interviewmaxxing prepare-batch --inventory inventory.json --workers 3 \
  --ai-routing --env-file /abs/env.local --writer-model anthropic/claude-opus-5.5 \
  --captcha-solver 2captcha --captcha-budget-usd 2.00
```

- **Off by default.** `--captcha-solver 2captcha` (or `IMX_CAPTCHA_SOLVER=2captcha` in the
  command's environment) turns it on for `apply`, `resume`, `prepare-batch`, `submit` and
  `submit-approved`. It needs `TWOCAPTCHA_API_KEY` beside the OpenRouter key: in the
  `--env-file` file, else in the file `IMX_OPENROUTER_ENV_FILE` names, else in the
  environment. Without the key the solver is simply off: every CAPTCHA stops the
  application as before, and nothing else changes. The key is never printed or recorded.
- **Budget.** `--captcha-budget-usd` (default 2.00, at most 100) caps what solving may
  spend: per run for `apply`, `resume` and `submit`; for `prepare-batch` and
  `submit-approved` once for the whole batch, whose jobs share `captcha-spend.jsonl` in the
  batch directory. A retry batch has its own; `submit-approved --batch BATCH_ID` writes to
  that batch's directory (unless `--batch-id` names another), so its solves count against
  the same file as the batch's preparation. Each solve holds USD 0.003 before 2Captcha is
  asked and then counts at the cost 2Captcha reports (a failed task costs nothing). A
  solve that would pass the cap is not asked for.
- **What is solved:** reCAPTCHA v2 (checkbox and invisible), reCAPTCHA v3, hCaptcha and
  Cloudflare Turnstile,
  - on a CAPTCHA page in front of the form and on a form step before the last, when the
    run meets them;
  - on the final form only when it is submitted. `prepare-batch` leaves it: the prepared
    application says "A CAPTCHA on this form must be solved in the browser before it can
    be submitted" and the review queue "CAPTCHA to solve when submitting". `submit` and
    `submit-approved` with the solver on answer it right before the approved submit (a
    token lasts about two minutes).
- **What is never solved** (2Captcha is not asked; you solve it in the browser as before):
  an invisible reCAPTCHA bound to the submit button (only its own callback, which sends
  the form, takes the token), a CAPTCHA page that also holds a form, anything in a
  `--browser opencli` session (it cannot write to the page), and image or text challenges
  that are none of the four widgets.
- **Solving never submits.** The token goes into the widget's answer field and nothing is
  clicked; a widget's callback is called only on a CAPTCHA page in front of the form that
  has nothing to fill.
  Submission stays behind `approve`, `IMX_ALLOW_SUBMISSION=1` and `--yes`.
- **Unsolved** (over budget, no answer from 2Captcha within 120 s, a 2Captcha error such as
  `ERROR_ZERO_BALANCE`, a token the page refuses, more than three solves in one run): the
  application is `needs_input` with "Solve the CAPTCHA", exactly as without the solver.
- **Records and cost.** Each attempt is a `captcha.solve` event of the application
  (`interviewmaxxing events APP`): widget kind, outcome, seconds, cost, 2Captcha's error
  code and the site's host, never the token or the key. An attempt that reached 2Captcha
  also counts as a provider call (`provider.budget`, purpose `captcha`), so the run's
  "Provider cost", the [batch report](#batch-report)'s cost columns and the dashboard
  include it.
