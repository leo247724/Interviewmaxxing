# interviewmaxxing-service (S1 + S2 + S3 + S3R)

Loopback-only HTTP service between the frontend's same-origin gateway (`apps/web`, F2) and the local executor. It maps canonical store state to the frontend's presentation models (`apps/web/lib/service/types.ts`) and hands all execution to the I1 runner. It is not a second executor or state machine. The canonical `ApplicationStore` is the only state authority.

Owner: queue-runtime. Package `interviewmaxxing-service`, import `interviewmaxxing_service`, script `interviewmaxxing-service`. Standard library HTTP only; no hosted platform, queue or extra dependency.

**S3R contract (for F3, published first).** The DTO changes below are final for this checkpoint:
[decisionTask](#decision-task-s3r), [health](#application-mode), [linked receipt](#linked-application-s3r) and [postingUrl](#posting-urls-s3r).

**Status: S3R checkpoint.** Real package integration is complete, and nothing in the route layer is simulated.
- **Applications (S1):** the real C2P `LocalCandidateStore`, the real I1 `create_runner` and headless Chromium against the separately running localhost mock ATS (`tests/service/test_acceptance.py`). The service pins the selected resume to each application before any run.
- **Pipeline (S2):** the real P1R `PipelineStore`.
- **Jobs and selection (S2):** the real J1 `JobStore`/`JobSearchService` and J2 `SelectionService`, with fixture source adapters and a fixture Jev transport in tests.
- **Test-only by default:** the service refuses to drive the browser at non-loopback sites (see [Application mode](#application-mode)).
- **Still to do:** frontend-driven end-to-end acceptance (F2/F3) and the final core, browser and package corrections (I1R, C4R3/O1R, J1R, J2R). See [Integration needs](#integration-needs) for the exact coverage of this checkpoint.

`integration.py` is the only module that names the C2P, J1, J2 and I1 APIs. See [Integration needs](#integration-needs).

## Run

### Service (development deployment, test-only)

```bash
IMX_HOME=/path/to/a/fictional/test-home \
IMX_SERVICE_ORIGIN=http://127.0.0.1:4317 \
IMX_SERVICE_APPLICATION_MODE=TEST_ONLY \
IMX_OPENROUTER_ENV_FILE=/path/to/ignored/env.local \
  uv run interviewmaxxing-service --port 8765
# -> interviewmaxxing-service listening on http://127.0.0.1:8765
```

- `IMX_HOME` must point at a fictional test home while testing. The real default home (`~/.interviewmaxxing`) holds the user's private profile and must not be used for test applications.
- `IMX_OPENROUTER_ENV_FILE` is only needed for Jev decisions. Without it, decisions are recorded with a `NOT_CONFIGURED` provider hold.
- Add `IMX_SERVICE_HEADLESS=1` for unattended runs. Leave it out when the user must sign in or solve a CAPTCHA in a visible window.

### Dashboard (apps/web, dashboard `4567d24`)

```bash
cd apps/web
IMX_BACKEND_URL=http://127.0.0.1:8765 IMX_WEB_ORIGIN=http://127.0.0.1:4317 \
  npm run dev -- --hostname 127.0.0.1 --port 4317
```

`IMX_WEB_ORIGIN` must equal the service's `IMX_SERVICE_ORIGIN`. The gateway checks the browser's `Origin` against it and sends it upstream.

| Variable | Default | Meaning |
| --- | --- | --- |
| `IMX_SERVICE_ORIGIN` | required | Exact frontend origin allowed to send mutations (loopback only) |
| `IMX_SERVICE_APPLICATION_MODE` | `TEST_ONLY` | `TEST_ONLY` or `LIVE`; see [Application mode](#application-mode) |
| `IMX_SERVICE_HOST` | `127.0.0.1` | Bind address; non-loopback is refused at startup |
| `IMX_SERVICE_PORT` | `8765` | `0` = ephemeral |
| `IMX_SERVICE_PUBLIC_BASE` | `/api/imx` | Path prefix the browser uses for evidence links |
| `IMX_SERVICE_HEADLESS` | `0` | `1` runs the runner's browser headless (tests); the default shows it for sign-in/CAPTCHA |
| `IMX_SERVICE_MAX_UPLOAD` | `10485760` | Resume upload limit (bytes) |
| `IMX_HOME`, `IMX_CANDIDATE_ID`, ... | see CONTRACTS.md §8 | Local data paths and the configured stable candidate id |
| `IMX_OPENROUTER_ENV_FILE` | unset | Explicit path to an ignored env file holding `OPENROUTER_API_KEY` (J2 `load_api_key`); otherwise the server's own environment. Never sent to the browser or logged. |
| `IMX_JOBS_DB` | `$IMX_HOME/jobs/jobs.sqlite3` | J1 listing store |

The service's own state (saved preferences and background tasks) is `$IMX_HOME/state/service.sqlite3` (mode `0600`). The pipeline is P1's `$IMX_HOME/state/pipeline.sqlite3`, and selection is J2's store.

On start the service marks submissions interrupted by an earlier process as `SUBMISSION_UNKNOWN` (`recover_interrupted_submissions`). Stop it with Ctrl-C or SIGTERM. A run still in progress is cancelled. The executor loop keeps running until the cancelled run's `finally` blocks finish (bounded), so the runner can await browser cleanup and release its claim. Only then does the loop stop. An interrupted submit stays durable `SUBMITTING`/`SUBMISSION_UNKNOWN` through the store's claim lease, so it is never retried.

### Application mode

`IMX_SERVICE_APPLICATION_MODE=TEST_ONLY` is the default for this development deployment.

- **Start.** `POST /applications` answers `422 {fieldErrors.applicationUrl}` for any URL whose host is not loopback (`127.0.0.1`, `::1` or `localhost`), before anything is recorded.
- **Resume and recheck.** `POST /applications/{id}/resume` and `/reconcile {"kind":"recheck"}` answer `403` for an application whose request URL is not loopback. They never start the browser for it.
- **Unaffected.** Job search, listings, preferences, Jev decisions, pipeline routes and user-reported reconciliation (no browser involved) work normally. Reading job sources is not an application.
- **Live mode.** `LIVE` must be set explicitly, and only for the separately authorized real application run.
- **Readiness for the UI.** `GET /healthz` reports it, with no private data:

  ```json
  {"status":"ok","service":"interviewmaxxing-service","contractVersion":"2","executor":"idle"|"busy",
   "runner":"available"|"unavailable","applicationMode":"TEST_ONLY"|"LIVE",
   "pipeline":"available"|"unavailable","jobs":"available"|"unavailable","selection":"available"|"unavailable"}
  ```

- **Limit.** The check is on the URL the service is given. Redirects the site itself performs happen inside the I1 browser.

## HTTP contract (for F2)

The Next gateway forwards `/api/imx/<path>` to `http://127.0.0.1:<port>/<path>`.

| Method and path | Request | Success |
| --- | --- | --- |
| `GET /healthz` | — | `{"status":"ok","service":"interviewmaxxing-service","contractVersion":"2","executor":"idle"\|"busy"}` (no private data) |
| `GET /candidate` | — | `CandidateView` |
| `POST /resumes` | raw bytes, see below | `201 ResumeDocumentView` |
| `POST /applications` | `StartApplicationInput` | `201` (new) or `200` (existing) `ApplicationView` |
| `GET /applications/{id}` | — | `ApplicationView` |
| `POST /applications/{id}/answers` | `AnswerInput` (+ optional `reuse`) | `ApplicationView`; invalid values in `needs.errors`, nothing saved |
| `POST /applications/{id}/resume` | `{}` | `ApplicationView` |
| `POST /applications/{id}/reconcile` | `ReconcileInput` | `ApplicationView` |
| `GET /applications/{id}/evidence/{evidenceId}` | — | the evidence file |

Errors are `{"error": {"code", "message", "fieldErrors"?}}` with the frontend's codes:

| Status | Code | When |
| --- | --- | --- |
| 400 | `invalid` | malformed JSON, unknown/missing body fields (`fieldErrors` by field), malformed id, query string |
| 403 | `invalid` | wrong `Host`, wrong or missing `Origin` on a mutation, `Sec-Fetch-Site: cross-site`, foreign `Origin` on a read |
| 404 | `not_found` | the application/evidence genuinely does not exist for the configured candidate, or no such route |
| 405 / 411 / 413 / 415 | `invalid` | wrong method, no `Content-Length`, body over the bound (JSON 64 KiB, upload `IMX_SERVICE_MAX_UPLOAD`), wrong content type |
| 409 | `conflict` | another application is using the browser; state does not allow the action (for example, resume of `SUBMISSION_UNKNOWN`); stale question ids (`fieldErrors` keyed by id); the application is claimed elsewhere |
| 422 | `invalid` | validated fields rejected (`applicationUrl`, `resumeId`, `firstName`, `lastName`, `email`, `location`, `resumeFile`, or unanswered question ids on resume) |
| 500 | `unknown` | unexpected error (logged by type only) |

### Transport rules the gateway must follow

- **Origin.** Every `POST` must carry `Origin: <IMX_SERVICE_ORIGIN>` exactly. The gateway validates the browser's own `Origin` first, then sends the configured approved origin upstream. `GET`s may omit `Origin`. There is no CORS; the browser never calls this service directly.
- **Host.** Connect to `127.0.0.1:<port>` (or `localhost:<port>`) so `Host` matches. Anything else is refused.
- **JSON.** Mutations use `Content-Type: application/json` and a `Content-Length`. Chunked bodies are refused.
- **Resume upload.** Translate the browser's `multipart/form-data` (`file`) into a raw body:
  - `Content-Type: application/octet-stream`
  - `X-Imx-Filename: <encodeURIComponent(file.name)>` (percent-encoded UTF-8; the header must be ASCII)
  - body = the file bytes, with `Content-Length`

  The candidate store decides what it accepts (`RESUME_UPLOAD_TYPES`: `.pdf .doc .docx .odt .rtf .txt .md`, with a matching content signature). The service first refuses names with `/`, `\`, control characters or a leading `.`. Errors come back in `fieldErrors.resumeFile`.
- **Evidence.** `href` values in views are `<IMX_SERVICE_PUBLIC_BASE>/applications/{id}/evidence/{evidenceId}`: an opaque id resolved from the canonical evidence record under the artifacts root. Paths are never exposed. Images and plain text are served `inline`. HTML, PDF, SVG and unknown types are served as `attachment`. Every response has `X-Content-Type-Options: nosniff` and `Content-Security-Policy: default-src 'none'; sandbox`. The gateway should forward `Content-Type`, `Content-Disposition`, `Content-Security-Policy` and `X-Content-Type-Options` for this route.
- Query strings are refused everywhere. Personal data travels only in bodies.
- `recheck` waits up to 25 s for the browser check, below the gateway's 30 s timeout. If the check is still running, the view says so in `uncertain.lastCheckResult`.

## Behaviour

- **Candidate.** `GET /candidate` shows the canonical profile (or empty strings before setup), the supplied resumes and `defaultResumeId` (the profile's resume). The configured `IMX_CANDIDATE_ID` is the identity; nothing is keyed by content.
- **Start.** Validates the URL, the chosen resume and the confirmed profile. `location` is the frontend's "City and region" field: `"Austin, TX"` is city `Austin`, region `TX`. Only an explicit third part is a country (`"Austin, TX, USA"`); no country is inferred from a region. More than three parts is a field error, never a guess. An unchanged location keeps the stored `PostalAddress` and an unchanged profile keeps its `verified_at`. The profile is saved through C2P only when it changed. The service then records the request through `store.record_request` **synchronously**, before any run, and returns the id (`201`) while the run continues on the executor thread. A repeat returns the same application (`200`), and nothing is sent again when it was submitted, is submitting or is unknown. Only one run uses the browser at a time. A request for another application while one is running gets `409` and is **not** recorded.
- **Questions.** `needs.questions`/`attestations` come from the latest packet's missing inputs. Labels, help text and options are the site's own, verbatim; placeholder and disabled options are never offered. Question ids are `q_` + a hash of (form scope, field id, field fingerprint), stable across reloads and new if the site changes the wording. Single checkboxes for consent/attestation are `attestations`; they show `accepted: true` only when the user accepted them.
- **Answers.** Every value is translated against the question's own options (machine `value` + visible `label`) and built with `UserInput.answering`, which keeps the exact wording, scope and fingerprint. All-or-nothing: any invalid value → `needs.errors`, nothing saved. An unknown/stale id → `409`. Blank values are skipped (draft save). Reuse defaults to **this application**. The optional request field `reuse: {questionId: "application"|"job"|"global"}` saves the answer through the candidate package's `save_answer` with that scope. Declining a required statement is refused.
- **Resume.** Allowed from `NEEDS_INPUT` (all required questions answered, else `422` keyed by id), `FAILED_RETRYABLE` and an interrupted pre-submission state. The run is started with browser actions allowed, so the user can sign in or solve a CAPTCHA in the visible window. Refused (`409`) for `SUBMITTING`, `SUBMISSION_UNKNOWN`, `SUBMITTED` and closed states.
- **Reconcile** (`SUBMISSION_UNKNOWN` only):
  - `recheck` runs the runner's browser-backed `reconcile`. Only proof on the site settles it. An inconclusive check keeps the lock and records `reconcile.checked`.
  - `user_found_confirmation` is recorded as the **user's** evidence (`USER_STATEMENT`) and reconciled with `ReconciliationMethod.USER_CONFIRMED`. The receipt's evidence is `source: "user"`, and the event says it was marked submitted on the user's report. It is never presented as site-confirmed.
  - `user_confirmed_not_received` is recorded as user evidence and an event. The state **stays `SUBMISSION_UNKNOWN`**: a user's report is not proof of non-submission, so it does not unlock a second submission (this intentionally differs from the F1 preview). Only a site recheck can establish `NOT_SUBMITTED`.
- **Failures.** If a run raises before submitting, the service moves the application to `FAILED_RETRYABLE` ("nothing was sent"), so the user can try again. A run that dies during submit is left to the store: it becomes `SUBMISSION_UNKNOWN` when the lease lapses, and status reads trigger that recovery.
- **Logs** contain method, route template and status only. Runner errors are logged by exception type.

### Application handoff links

`StartApplicationInput` accepts optional `pipelineEntryId: string | null` and
`listingId: string | null`. URL-only starts keep their existing behavior.

- A supplied entry must belong to the configured candidate. A supplied listing must
  exist. Both IDs must resolve to the same saved job; a manual entry without a listing
  can match by its saved URL. Known posting/application URLs must match the handoff.
  Shared search `sourceUrl` values are never matching evidence.
- Invalid, missing, foreign or mismatched IDs/URLs return `422` with
  `fieldErrors.pipelineEntryId`, `listingId` or `applicationUrl`, before recording or
  dispatching. An entry already linked to a different application returns `409` and
  keeps that link.
- A listing-only handoff tracks the listing once. After recording the canonical
  application and pinning the resume, the service writes the pipeline link before
  browser dispatch. Repeats keep the same application and card.
- If linking fails, no browser run starts. The recorded/pinned request remains safe
  to retry; the same handoff completes the missing link idempotently. A concurrent
  entry edit returns `409` and asks for a reload. Unexpected storage failure returns
  `500`; the existing entry and application are not silently replaced.
- `GET /pipeline` reads the linked application's actual state and receipt authority,
  including `SUBMISSION_UNKNOWN`, site confirmation and user-reported confirmation.

### Resume pinning (S3)

`POST /applications` pins the exact selected `ResumeArtifact` to the application with `ApplicationStore.pin_resume`. It does this synchronously after `record_request` and before dispatch.

- **Which artifact.** The resume from the C2P upsert result when the profile changed, else C2P `get_resume(candidate, resumeId)`. The service never rereads the global profile to choose it.
- **First writer wins.** A repeated request (even with another resume selected) or a restart keeps the original pin. The I1 runner then always uploads the pinned file.
- **In the view.** `ApplicationView.resumeFileName` shows the pinned file, and the event `document.resume_pinned` appears in `events`.

### Pending questions (S3)

`needs.questions`/`attestations` come from the I1 runner's `application.needs_input` event (`metadata.missing_inputs`, the same data as I1 `pending_inputs`). They fall back to the latest packet only for records without it. I1's inconclusive recheck event `reconcile.unconfirmed` sets `uncertain.lastCheckedAt`/`lastCheckResult`.

### Receipt confirmation (S1R, additive DTO fields for F2)

`ApplicationView.receipt` (`SubmissionReceiptView`) carries two explicit fields in addition to the F1 shape:

```ts
confirmationMethod: "SUBMISSION_OBSERVED" | "SITE_CONFIRMATION" | "ATS_CANDIDATE_PORTAL" | "CONFIRMATION_EMAIL" | "USER_CONFIRMED";
confirmationAuthority: "site" | "user";
```

- `SUBMISSION_OBSERVED`: the site's acceptance was observed right after submit (`Receipt.reconciliation_method` is null).
- The other methods are the canonical `ReconciliationMethod` that settled a `SUBMISSION_UNKNOWN`.
- `confirmationAuthority` is `"user"` exactly when the method is `USER_CONFIRMED`, and `"site"` otherwise.

The frontend must label a receipt from these fields, not from its evidence list. A user-confirmed receipt can still list site artifacts (for example, screenshots of the uncertain page, `source: "site"`). They did not confirm anything, so the receipt stays user-reported.

## Pipeline, jobs and selection routes (S2, for F3)

These implement the routes proposed in `apps/web/README.md` (dashboard `816afa1`). The DTOs match `apps/web/lib/pipeline/types.ts` and `lib/jobs/types.ts` field for field, plus the additive fields marked **(+)**. All transport rules above apply unchanged: loopback `Host`, exact `Origin` on every `POST`, JSON only, no query strings, and bounded bodies. The import preview body may be up to 4 MiB; every other body is limited to 64 KiB.

| Operation | Method and path | Body | Success | Notes |
| --- | --- | --- | --- | --- |
| board | `GET /pipeline` | — | `PipelineBoardView` | P1 lanes in order; entries in creation order |
| create | `POST /pipeline/entries` | `PipelineEntryInput` | `201 PipelineEntryView` | `fields` keys must be reference keys; `422` per field |
| update | `POST /pipeline/entries/{id}` | `PipelineUpdateInput` | `PipelineEntryView` | partial; stale `revision` → `409` |
| move | `POST /pipeline/entries/{id}/move` | `{revision, lane}` | `PipelineEntryView` | unknown lane → `422 {lane}`; stale → `409` |
| import preview | `POST /pipeline/import/preview` | `{format, fileName, content}` | `ImportPreviewView` | parsed by P1; kept in memory 30 min under `previewId` |
| import commit | `POST /pipeline/import/{previewId}/commit` | `{}` | `ImportReceiptView` | rows with errors → `409`, nothing imported; expired/used preview → `404` |
| preferences | `GET /selection/preferences` | — | `SearchPreferencesView` | defaults until saved |
| save preferences | `POST /selection/preferences` | preferences without `fingerprint` | `SearchPreferencesView` | persisted as canonical `SelectionPreferences` |
| start search | `POST /jobs/search` | preferences without `fingerprint` (the query) | `202 SearchRunView` | id recorded before queuing |
| search status | `GET /jobs/search/{runId}` | — | `SearchRunView` | poll until `finishedAt` |
| listings | `GET /jobs` | — | `ListingsView` | ranked (see below); `lastRun` is the latest search |
| listing **(+)** | `GET /jobs/{listingId}` | — | `ListingView` | |
| decide | `POST /selection/jobs/{listingId}` | `{}` | `200 ListingView` (done) or `202 ListingView` (still deciding) | explicit user action only |
| track | `POST /jobs/{listingId}/track` | `{}` | `201`/`200 ListingView` with `pipelineEntryId` | one card per listing |

### Additive DTO fields (+) and mapping notes

- `SearchPreferencesView.roleFocus` (+) is a string: the canonical D0 `role_focus`, the semantic description Jev judges responsibilities against. It defaults to D0 `DEFAULT_ROLE_FOCUS` (performance marketing operator).
  - It is optional in request bodies; leaving it out keeps the saved value.
  - It is part of `SelectionPreferences` (so it changes `fingerprint` and J2's cache key) and of the search query (`JobSearchQuery.role_focus`).
  - `titlePhrases` are search seeds, not an exact-title allowlist. The defaults are D0's performance-marketing seeds, most specific first.
- `SearchPreferencesView.locationPriority` (+) takes `"STRONGLY_PREFER_ONSITE_HYBRID" | "BALANCED" | "PREFER_REMOTE"`. It is the canonical D0 `LocationPriority` and defaults to strongly preferring onsite/hybrid.
  - In request bodies it is optional. Leaving it out keeps the saved value.
  - It is part of `SelectionPreferences`, so it changes `fingerprint` (earlier decisions become `stale`), reaches Jev and J2's decision cache, and sets the J1 search leg order and budget.
- `ListingView.locationTier` uses the frontend's `LocationTier` strings exactly: `"ONSITE_HYBRID_TARGET" | "REMOTE_ELIGIBLE" | "REMOTE_UNCONFIRMED" | "OUTSIDE_TARGET" | "UNRESOLVED"`. They are mapped from J2 `check_location`:

  | J2 `check_location` | `locationTier` |
  | --- | --- |
  | `ONSITE_ACCEPTED` | `ONSITE_HYBRID_TARGET` |
  | `REMOTE_REGION_MATCH` | `REMOTE_ELIGIBLE` |
  | `REMOTE_NEEDS_ELIGIBILITY` | `REMOTE_UNCONFIRMED` |
  | `ONSITE_MISMATCH` | `OUTSIDE_TARGET` |
  | `REMOTE_NOT_WANTED` | `OUTSIDE_TARGET` |
  | `UNKNOWN` | `UNRESOLVED` |

- `ListingView.priorityTier` (+) takes `"PREFERRED" | "EQUAL" | "SECONDARY" | "UNRANKED"`, from J2 `location_tier` under `locationPriority`. `ListingView.rankReason` (+) is a string. All three are `null` when the selection package is not installed.
- **Listing order:**
  0. The saved location priority is applied by J1 (`list_listings(rank_for=preferences)`) *before* the 500-listing limit, so newer remote listings cannot crowd older Austin onsite/hybrid ones out.
  1. Closed listings last.
  2. Then priority tier. With the default priority, Austin onsite/hybrid is `PREFERRED` and eligible US-wide remote is `SECONDARY`, so remote roles are still listed, never excluded.
  3. Then stated pay against the floor: meets, unknown, below. Unknown pay is not demoted below pay that misses the floor.
  4. Then most recently observed.
- `PipelineEntryView.fields` holds the 23 P1 reference keys, and blank is `null`.
  - `origin` is `import` when P1 has provenance, `jobs` when linked to a listing, and `manual` otherwise.
  - `history` maps P1 kinds `created`, `imported`, `moved` and `stage_edited`; `stage_edited` becomes `edited`, with a readable summary.
  - `provenance.importedValues` is P1R's immutable `initial` row (first import), keyed by workbook header.
  - `provenance.importId` is the receipt id of that document, and `sourceDigest` is the workbook digest (else the document digest).
  - Additive (+) `provenance` fields: `sourceId` (P1R logical source), `latestImportedValues` (P1R `latest`), `firstImportedAt` and `versionCount` (P1R `source_versions`).
- **Import source (+).** `ImportInput.sourceId` is an optional string. It names the stable logical source of an upload, chosen by the user (for example `"numbers-pipeline"`), and re-imports of the same tracker reuse it.
  - It must match `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$`, else `422 {sourceId}`.
  - A CSV (or a JSON export that declares no `source.sourceId` or workbook path) without it previews with a file-level error row. Commit is refused, because the service never derives a source from the file digest or the file name.
- **Lane ids** are P1's: `saved, applied, scheduling, interviewing, assessment, follow-up, decision, offer, closed`.
  - **Incompatibility:** the frontend README proposed `follow_up`, but P1 uses `follow-up`. Use the ids from `GET /pipeline`.
- `PipelineEntryView.application` is the only submission state shown on a card, read from the canonical `ApplicationStore` by the linked `application_id`. Moving a card to "Applied" or "Offer" creates or changes no application.
- **Import preview.** Rows with issues become `action: "error"` with `errors[{field, message}]`, where `field` is the reference key or `row`. File-level issues appear as row `0`. An unreadable file answers `422 {content}`.
- **Search.**
  - `SearchRunView.results` covers every requested source, in order. Sources show `QUEUED`, then `RUNNING`, then J1's final state.
  - `NEEDS_USER` carries `userAction` and `sessionName` (`imx-jobs-<source>`). `BLOCKED` and `ERROR` carry a message and are never an empty success.
  - A search interrupted by a restart shows its unfinished sources as `ERROR`.
  - Searches run one at a time, on their own worker, independent of the application browser. Repeating an identical query while it runs returns the same run. A different query while one runs gets `409`.
- **Decisions.**
  - Decisions run on their own single worker, with at most 10 queued. Concurrent requests for the same listing and preferences join one decision; the provider is called once.
  - The call waits up to 20 s for the decision (the gateway allows 30 s). `202` means it is still running; poll `GET /jobs/{id}` and read `decisionTask` (below).
  - A failed decision no longer answers `502`: the response is `200` with `decisionTask.state: "FAILED"` and a plain `error`. Nothing is recorded as a selection.
  - Decisions are strictly per candidate. A decision recorded for another candidate is never shown, linked to a pipeline card or reused from J2's cache, even when the listing and inputs are identical.
  - `selection.unresolved` lists facts J2 could not establish: pay, location or remote eligibility, missing profile, and insufficient evidence.
  - A missing key, or a provider failure, is recorded as J2's `PROVIDER_ERROR` hold (for example `NOT_CONFIGURED`), never an APPLY.
  - Jev receives only J2's minimal `CandidateEvidence.from_profile` projection of the complete candidate profile, or nothing, which is held as `MISSING_PROFILE`.
- **Track** creates one P1 card per listing, in the first lane. It copies only observed facts:
  - company and title;
  - the arrangement as text;
  - location into Location/commute;
  - pay bounds only when stated in USD per year (the raw text goes into Comp basis);
  - the source names;
  - the listing's application URL, else its posting URL;
  - the current selection id.

  Tracking, deciding and searching never create an application. Applying still goes through `POST /applications`, with its duplicate check.

### Accepted listing aliases

J1's persisted alias map keeps service joins intact when a stronger direct-source
observation becomes a listing's canonical ID. Pipeline card lookup/tracking, latest
candidate decisions and decision-task polling include those accepted historical IDs.
A queued decision requested before the merge stays pollable and repeat requests join
it. Existing card IDs and stored listing provenance remain intact; tracking does not
create another card for the new canonical ID. Similar titles, companies or shared
search URLs are never treated as aliases by the service.

### Decision task (S3R)

`ListingView.decisionTask` (+) is the latest explicit decision request for this listing and candidate, from the service's durable task record (`$IMX_HOME/state/service.sqlite3`). It is `null` when no decision was ever requested. It uses the service's existing task vocabulary:

```ts
interface DecisionTaskView {
  id: string;                       // "dec_<hex>", stable before the request returns
  state: "QUEUED" | "RUNNING" | "DONE" | "FAILED" | "INTERRUPTED";
  error: string | null;             // plain language, only for FAILED and INTERRUPTED
  resultId: string | null;          // DONE only: the SelectionView.id it produced
  requestedAt: string;              // ISO-8601 UTC
  updatedAt: string;
}
```

- **Polling.** Poll `GET /jobs/{listingId}` (or read `GET /jobs`) until `state` is `DONE`, `FAILED` or `INTERRUPTED`. `DONE` means the decision finished, even when `resultId` equals an earlier `selection.id` (J2's cache answered); stop polling then.
- **Coalescing.** A repeated request while one is `QUEUED`/`RUNNING` returns the same `id`.
- **Restart.** A service restart marks unfinished tasks `INTERRUPTED` with `error: "The service stopped before this decision finished. Ask again."`.
- **Failure.** `FAILED` keeps `selection` unchanged (the previous decision, if any, or `null`).

### Linked application (S3R)

`PipelineEntryView.application` (`LinkedApplicationView`) carries two additive fields, with exactly the receipt DTO's strings:

```ts
confirmationMethod: "SUBMISSION_OBSERVED" | "SITE_CONFIRMATION" | "ATS_CANDIDATE_PORTAL" | "CONFIRMATION_EMAIL" | "USER_CONFIRMED" | null;
confirmationAuthority: "site" | "user" | null;   // null when there is no receipt
```

`"user"` exactly for `USER_CONFIRMED`, even when the receipt lists older site artifacts. A user's report that nothing arrived never unlocks an uncertain application: its linked `state` stays `SUBMISSION_UNKNOWN` with no method or authority.

### Posting URLs (S3R)

- `ListingSourceView.postingUrl` (+) is the canonical `posting_url`: this posting's own page on that source, or `null`.
- `ListingView.postingUrl` (+) is the listing's own `posting_url`, or `null`.
- `sourceUrl` stays observation provenance and may be a shared search page. It is never a navigation or application link.
- The link order for navigating or applying is `applicationUrl`, then `postingUrl`, else none. Tracking a listing sets the card's `applicationUrl` the same way; with neither, the card's `applicationUrl` is `null` and the UI must ask for it.

## Public Python API

```python
from interviewmaxxing_service import (
    ServiceConfig, PresentationService, Dispatcher, make_server,
    ApplicationExecutor, ServiceInteraction, CandidateGateway, ResumeEntry,
)

config = ServiceConfig.from_env()            # or ServiceConfig(paths=LocalPaths, allowed_origin=..., port=0)
dispatcher = Dispatcher(executor_factory)    # Callable[[ServiceInteraction], ApplicationExecutor]
service = PresentationService(config, candidates=gateway, dispatcher=dispatcher)
service.recover()
server = make_server(service); server.serve_forever()
```

`ApplicationExecutor` (what the service needs from I1):

```python
class ApplicationExecutor(Protocol):
    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome: ...
    async def resume(self, application_id: str) -> ApplyOutcome: ...
    async def reconcile(self, application_id: str) -> ApplyOutcome: ...   # browser recheck, never resubmits
```

`CandidateGateway` (what the service needs from C2P; `integration.LocalCandidateGateway` implements it over `LocalCandidateStore`):

```python
class CandidateGateway(Protocol):
    def setup(self, candidate_id: str) -> CandidateSetupState: ...   # candidate_setup(): identity | None, resumes, selected id, complete
    def store_resume(self, candidate_id: str, *, filename: str, content: bytes) -> ResumeEntry: ...   # ResumeRejected -> resumeFile
    def upsert_profile(self, candidate_id: str, *, identity: CandidateIdentity, resume_id: str) -> CandidateProfile: ...
        # ResumeNotFound -> resumeId; CandidateProfileInvalid -> 409, nothing written
    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None: ...
```

C2P decides the stored file name (a safe ASCII name) and media type, checks the content signature and applies the size bound (`IMX_SERVICE_MAX_UPLOAD` is passed as `max_resume_bytes`). A resume an imported profile already referenced has no upload time, so `uploadedAt` shows its file's modification time. Resumes are listed newest upload first, and the profile's own resume last.

## Integration needs

Merged into this branch and used directly (committed checkpoints only):

| Package | Checkpoint | Used through |
| --- | --- | --- |
| I1 runner | `d43ce6b` | `create_runner(paths, *, headless, interaction)`; `ApplicationStore.pin_resume`/`pinned_resume`. The service also serializes runs itself. |
| C2P/C2P2 candidate | root `caae823` | `candidate_setup`, `store_resume`, `get_resume`, `upsert_profile`, `save_answer`, `load` |
| J1 jobs | J1S `1d8f2ee` | `JobStore.get_listing`, `list_listings(limit=, rank_for=)`, `JobSearchService(...).run(single-source query, detail_limit=)`, `OpenCliTransport`, `default_db_path` |
| J2 selection | J2S `0f58421` (sends `role_focus`) | `SelectionService.select(..., use_cache=)`, `.is_current`, `SelectionStore.latest/history/get`, `CandidateEvidence.from_profile`, `JevClient(load_api_key())`, `check_location`, `location_tier`, `location_priority_reason` |
| P1R2 pipeline | `e5c14c7` | `PipelineStore` (incl. `source_versions`), `parse_import(..., source_id=)`, `TrackingFields`, `REFERENCE_FIELDS` |
| Browser | root (C4) | via I1 |

**Checkpoint coverage.** The S3R service tests and the real HTTP → I1 → headless Chromium → mock ATS acceptance ran on exactly these commits. They did **not** cover:

- the final core runner corrections (I1R);
- the browser corrections C4R3 and O1R;
- the in-progress J1R (region matching in `rank_for`) and J2R (candidate-scoped store APIs).

Final MVP end-to-end acceptance still needs those, plus the dashboard-driven run.

**Adapter dependencies on in-progress corrections:**

- **J2R candidate scoping.** Committed J2 (`0f58421`) `SelectionStore.latest/get/history/find_cached` take no candidate. The service adapter therefore:
  - filters `history(listing_id)` by `selection.candidate_id` for listing views, which parses that listing's full history once per request;
  - checks the candidate on every `get`;
  - re-runs `select(..., use_cache=False)` when J2's evidence-keyed cache returns another candidate's decision, and refuses the result if it still belongs to another candidate.

  When J2R publishes `latest(listing_id, *, candidate_id)` / `get(selection_id, *, candidate_id)`, the adapter uses them automatically (it detects a `candidate_id` parameter) and still verifies the candidate. A batched `latest_many(candidate_id, listing_ids)` would remove the remaining per-listing query; it is requested, not required.
- **J1R region matching.** Pre-limit ranking is J1's `rank_for`. The service's own finer ordering (J2 tier, pay, recency) is applied only within the returned 500.
- **J1 (optional).** `run(query, *, run_id=None, on_source=callback)`. With it, one J1 run could carry the service task id instead of the service running J1 once per source.

**Dependencies.** `pyproject.toml` pins `interviewmaxxing-core`, `-candidate`, `-pipeline`, `-jobs`, `-selection` and `-cli` at `==0.1.0` (workspace sources). J1/J2 are still imported lazily in `integration.py`, so the route layer has no hard import on them. The root lock is core/coordinator-owned and must include this member.

## Tests

```bash
uv venv .venv-task --python 3.12
uv pip install --python .venv-task/bin/python --no-sources -e packages/core -e packages/candidate \
    -e packages/pipeline -e packages/generation -e packages/browser -e packages/jobs \
    -e packages/selection -e apps/cli "playwright==1.62.0" pytest ruff mypy
uv pip install --python .venv-task/bin/python --no-deps --no-sources -e apps/service
.venv-task/bin/python -m pytest tests/service        # includes the real Chromium acceptance
.venv-task/bin/ruff check apps/service tests/service && .venv-task/bin/mypy --strict apps/service/src
```

`tests/service` starts the real server on an ephemeral loopback port over real SQLite stores in a temporary `IMX_HOME`:

- `test_http_flow`, `test_http_boundary`, `test_mapping`: the S1 application routes, with a scripted runner that performs the runner recipe's store operations against a fictional site.
- `test_executor`: the executor waits for a cancelled run's awaited cleanup before stopping.
- `test_candidate_integration`: the real C2P store.
- `test_pipeline_routes`: the real P1 store, including CSV import preview, commit and reimport.
- `test_jobs_routes`: the jobs/selection orchestration over protocol fakes that produce canonical D0 records.
- `test_review_s3r`: the S3R corrections:
  - candidate isolation, with real J2 and a fictional transport, including the cross-candidate cache case;
  - the decision task (running and coalesced, in-process failure, cache hit with the same result id, restart interruption);
  - Austin ranked before the listing limit (real J1);
  - linked receipt authority, including a user report that does not unlock;
  - `postingUrl`, and that track never uses a search page;
  - one pipeline and one decision lookup per listing page.
- `test_package_integration`: the real J1 store and search service with fixture source adapters, and the real J2 selection service with a fixture Jev transport.
- `test_acceptance` (marked `slow`): HTTP → real I1 runner → headless Chromium → `scripts/mock_ats.py` in its own process, with a fictional profile in a temporary `IMX_HOME`. It covers:
  - receipt, server-side acceptance count, uploaded file digest and a repeat request;
  - A/B resume pins across a profile change and a restart, with missing answers answered after the restart;
  - an uncertain submission that stays locked, an inconclusive recheck, and a site reveal reconciled.

Playwright must be the root-locked `1.62.0`, which matches the cached browsers.

Everything is fictional and local. No live source is browsed, no Jev credit is spent, and nothing is submitted.
