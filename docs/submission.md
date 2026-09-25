# Submitting approved applications

**Nothing is submitted unless three things hold together:** you approved the prepared
application (`interviewmaxxing approve APP`), the command runs with
`IMX_ALLOW_SUBMISSION=1`, and you pass `--yes`. `apply`, `resume`, `prepare-batch`
and the dashboard never submit; they prepare. What is submitted is exactly the
packet you approved: the submission run fills it again from the store and never
resolves, generates or re-reads an answer from your profile.

This path is built and tested against the localhost mock ATS only
(`scripts/mock_ats.py`). No real employer submission has been authorized or made.

## The flow

```bash
interviewmaxxing apply URL --headless          # prepare: stops at the final review step (exit 3)
interviewmaxxing status APP                    # "prepared: final review step reached; nothing was submitted"
interviewmaxxing approve APP                   # approve the prepared answers (lists them); submits nothing
IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit APP --yes   # submit exactly what you approved, once
interviewmaxxing receipt APP                   # the site's confirmation
```

For many applications, prepare them with `prepare-batch`, review and approve each one,
then:

```bash
IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit-approved --batch BATCH_ID --slots 3 --yes
IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit-approved --all-approved --slots 3 --yes
interviewmaxxing batch-report BATCH_ID         # prepare totals plus a Submissions table
```

1. **Prepare.** A prepare-only run records the no-submit restriction
   (`application.preparation_only`) before it opens the browser, fills every step and
   stops at the final review step as `NEEDS_INPUT` with a `preparation.ready` event. That
   event names the prepared packet and, for every step the run filled, the step's packet
   and the questions it answered (`steps`, see [Events](#events)).
2. **Review.** Check the filled form's evidence (`$IMX_HOME/artifacts/APP/`), the answers
   (`status APP --json`, the dashboard's review lane) and the event history.
3. **Approve.** `approve APP` approves the packet of the preparation the application is
   stopped at (`--packet PKT` names it explicitly; any other packet is refused). It prints
   every approved answer and records `application.approved`. Approving submits nothing and
   does not lift the restriction.
4. **Submit.** `submit APP --yes` (with `IMX_ALLOW_SUBMISSION=1`) records
   `application.submission_authorized` for the approved packet, which lifts the restriction
   for exactly that packet, and runs the submission. Only an authorized packet can take a
   submission attempt (`begin_submission` and an SQL trigger both check it).
5. **Afterwards.** `SUBMITTED` comes only from the site's confirmation tied to the job and
   writes a receipt. An uncertain outcome (`SUBMISSION_UNKNOWN`) is never retried; `reconcile
   APP` re-reads the site. A form that changed, or answers the site rejected, end the run as
   `NEEDS_INPUT` with the approval withdrawn: prepare it again (`resume APP`), answer what is
   asked, review and approve the new preparation.

`submit` without `IMX_ALLOW_SUBMISSION=1` prints how to enable it and exits 4. Without
`--yes`, or without a valid approval, it exits 4 too; nothing is opened in any of these
cases. The variable is read only by `submit` and `submit-approved`. `prepare-batch` removes
every `IMX_*` variable from its workers' environment, so a prepare worker never sees it.

## What the submission run does

The run opens the application URL like any run (same browser profile lock, store claim,
pinned resume and selected-job check), then for each form step:

1. It looks up the step's approved packet. A step that was not approved stops the run.
2. **Before filling**, it compares the form with the approved step: every question must be
   there with the same fingerprint (label, help text, placeholder, control type and
   options), the same required flag and the same options, no question may be new (required
   or optional), and the final step must still be the approved final step.
3. It fills the approved packet unchanged: same packet id, values and provenance. Only its
   binding to this inspection is updated: the step URL (a multi-step form puts a new draft
   id in it) and each answer's semantic type, which is the runtime's reading of an
   unchanged question and not part of the site's form.
4. A value that does not read back (`VERIFICATION_MISMATCH`), a lookup whose approved value
   no longer commits (`NEEDS_CHOICE`), or an approved option the site now disables stops the
   run. A step that changes while filling is inspected and compared again.
5. It advances with the same unambiguous-Next rules as preparation. On the final step it
   calls `begin_submission` with the approved packet id, clicks the final submit control
   once and records what the site shows (`record_submission_outcome`). These operations and
   the `is_final_step` checks are the same as for any submission.

| What happened | State | Approval |
| --- | --- | --- |
| Confirmation tied to this job | `SUBMITTED`, receipt saved | used |
| Submit dispatched, no tied confirmation, or interrupted during the submit | `SUBMISSION_UNKNOWN` (never retried; `reconcile`) | — |
| A step that was not approved; a new, missing or changed question; changed options or required flag; the final step moved; a value that does not read back; a lookup that no longer commits; an approved option now disabled | `NEEDS_INPUT`: "The form no longer matches the approved application: …" | withdrawn |
| The site showed the form again with validation errors after the submit | `NEEDS_INPUT`: "… the site did not accept the approved answers (…)" (a `validation.rejected` event makes the next preparation ask again) | withdrawn |
| Sign-in, CAPTCHA, or a custom control you set yourself while preparing | `NEEDS_INPUT` with the action (or, with `--act` in a visible browser, the run waits for you and continues) | kept |
| A field that could not be operated (`FAILED`), a browser error before the submit, an ambiguous Next control | `FAILED_RETRYABLE`, nothing submitted | kept; `submit` again retries |
| The same questions read with a semantic type whose answer must come from a saved answer or from you | `FAILED_RETRYABLE`: "The approved answers cannot be filled as the page is read this time" | kept; submit with the runtime options it was prepared with |
| The job is closed / already applied | `FAILED_PERMANENT` / `DUPLICATE` | — |

A withdrawn approval writes `application.approval_invalidated` and, when the submission was
authorized, `application.preparation_only` again: the restriction is back until the
application is prepared and approved again. Approving again needs a new preparation, because
the application is no longer stopped right after one.

## The store's rules

`ApplicationStore` (`packages/core`) is the only authority; the CLI, the runner and any
future dashboard action call these operations (see `CONTRACTS.md` §7):

- `is_preparation_only(app)` is true while the application has an
  `application.preparation_only` event and no authorization lifts it. An authorization
  lifts it only while the latest `application.submission_authorized` is newer than every
  `application.preparation_only` event and names the packet of the latest
  `preparation.ready`.
- `approve_submission(claim, packet_id=, approver=)` requires `NEEDS_INPUT`, the latest
  `preparation.ready` right behind the current stop (only the run's own re-inspection and the
  stop after it), `packet_id` equal to its packet, and every prepared step's packet present
  and complete. Otherwise `SubmissionBlocked`. Approving the same packet again returns the
  existing approval.
- An approval is valid while it names the latest `preparation.ready` and no
  `application.approval_invalidated` followed it (`submission_approval`, `approved_packet`).
  A new preparation invalidates it.
- `authorize_submission(claim)` requires a pre-submission state and a valid approval.
- A prepare-only run calls `require_preparation_only`, which records the restriction again
  on an authorized application, so a prepare-only run after an approval never submits.
- `begin_submission(claim, packet_id=)` refuses a preparation-only application and, for an
  authorized one, any packet other than the authorized one. The SQL trigger
  `preparation_blocks_submission` enforces the same rule for any writer. A database created
  before this change has its trigger replaced on open; the schema version stays 4, and older
  code still refuses these attempts in Python.

## Events

| Event | Written by | Metadata |
| --- | --- | --- |
| `application.preparation_only` | store (`require_preparation_only`, `invalidate_approval`) | `submission_authorized: false`; `reason` when an approval was invalidated |
| `preparation.ready` | runner (prepare-only run at the final review step) | `form_url`, `form_step`, `form_fingerprint`, `packet_id`, `submitted: false`, `browser_location`, `captcha_pending`, `steps: [{form_step, packet_id, form_url, form_fingerprint, final, fields: [{id, fingerprint, required, semantic_type, control_type, options, label}]}]` (`options` is a digest of the option values and labels, or null; `label` is the question's first line, at most 80 characters) |
| `application.approved` | store (`approve_submission`) | `packet_id`, `approver` (`cli:<login>` from the CLI), `form_step`, `form_url`, `form_fingerprint`, `preparation_event_id`, `steps: [{form_step, packet_id}]`, `captcha_pending` |
| `application.submission_authorized` | store (`authorize_submission`) | `packet_id`, `approval_event_id`, `approver` |
| `application.approval_invalidated` | store (`invalidate_approval`, called by the runner) | `packet_id`, `approval_event_id`, `reason`, `details` (what differed, at most 20) |
| `application.submitting` → `application.submitted` / `application.submission_unknown` / … | store (`begin_submission`, `record_submission_outcome`) | `attempt_id`, `attempt_number`, `packet_id` (the approved packet); then the observation |

A preparation recorded before this change has no `steps`. It can still be approved: the
approval takes the latest packet saved for each step of the preparing run, and the
submission run then requires each step to show exactly the questions of that packet's form
fingerprint (ids, wording and options; the step URL may differ), and every required
question to have an approved answer.

## Exit status

| Command | 0 | 3 | 4 | 5 |
| --- | --- | --- | --- | --- |
| `approve APP` | approved | — | not stopped at a completed preparation, another packet, open questions, application busy | — |
| `submit APP --yes` | submitted | not submitted: needs input, form changed, stopped retryable, closed | no `IMX_ALLOW_SUBMISSION=1`, no `--yes`, no valid approval, already submitted, duplicate, application busy | uncertain |
| `submit-approved … --yes` | every selected application submitted, or nothing to submit | some not submitted (blocked, needs input, error) | no `IMX_ALLOW_SUBMISSION=1` or no `--yes` | some uncertain |

`1` is an error (no state database), `2` a usage error (for example an unknown `--batch`),
`130` an interrupt. With `--json`, `submit` prints the `ApplyOutcome`, also for a refusal
once the application is known.

## Ledger and report

`submit-approved` selects the candidate's applications that have a valid approval and a
pre-submission state: those in the named prepare batch's ledger (`--batch`), or all of them
(`--all-approved`). It runs one `interviewmaxxing submit APP --yes --json` process per
application, at most `--slots` at a time, each slot with its own browser profile
(`$IMX_HOME/browser-workers/w<slot>`), and kills a process group that exceeds
`--per-job-timeout`. Each result is appended to `$IMX_HOME/batches/<id>/ledger.jsonl`, where
`<id>` is `--batch-id`, else the `--batch` id, else `approved-<UTC time>`:

```json
{"kind": "submission", "batch_id": "b1", "source_batch_id": "b1", "application_id": "app_…",
 "approved_packet_id": "pkt_…", "listing_id": "lst_…", "company": "…", "title": "…",
 "application_url": "…", "attempt": 1, "worker_slot": 0, "state": "SUBMITTED",
 "outcome": "submitted", "receipt_id": "sub_…", "confirmation_reference": "…",
 "message": "…", "exit_code": 0, "started_at": "…", "finished_at": "…", "duration_s": 12.3}
```

`outcome` is `submitted` (with the receipt's id, its attempt id), `uncertain`, `blocked`,
`needs_input` or `error`. It is read back from the store after the process ends, so a
submission stopped by the timeout while submitting is `uncertain`, never `error`. Prepare
lines and submission lines share the file without being mistaken for each other. Running the
same ledger id again never launches an application it records as submitted or uncertain.
`submission-summary.json` holds the run's summary (prepare-batch's `summary.json` is left
alone). `prepare-batch --retry` never re-prepares an application that still has a valid
approval (skipped as `approved (left to submit-approved)`), and its ledger reader does not
count submission lines as unreadable. `batch-report` adds a Submissions table (counts by outcome, receipts, uncertain
applications) and no longer says "nothing was submitted" once one was.

## Limitations

- Verified only against the localhost mock ATS; nothing was submitted to a real employer.
- A question that differs only in how the runtime probed or read it (a menu probe that timed
  out, so a select reads as a custom control) counts as a changed question: the approval is
  withdrawn and the application must be prepared again. The refusal is safe, but it costs a
  new preparation.
- A custom control you set by hand while preparing is not in the packet. The submission run
  asks you to set it again (`submit APP --yes --act` in a visible browser); `submit-approved`
  runs headless and records such applications as `needs_input`.
- Questions that appear only after another answer is filled stop preparation already; the
  submission run stops the same way.
- A multi-step site that keeps drafts gets a new draft from the submission run; the draft
  the preparing run saved stays behind on the site.
- The dashboard has no approve action yet; it is planned on top of `approve_submission`.
