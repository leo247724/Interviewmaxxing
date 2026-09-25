# Interviewmaxxing web — application desk, pipeline and jobs

Next.js 16 / React 19 / TypeScript frontend for Interviewmaxxing. It has four views:

- **Desk:** the supplied-URL application flow (`ARCHITECTURE.md` §2 and §17). This development build requires verified `TEST_ONLY` readiness and a loopback application URL before starting or resuming browser execution. The desk shows progress, asks for missing required answers or direct user statements, and ends with a receipt or an accurate blocked/uncertain state.
- **Pipeline:** the user's own tracker, using the 23-column reference workbook schema.
- **Jobs:** search across job sources, with Jev APPLY/SKIP/REVIEW decisions.
- **Review:** the review-and-submit lane. The Prepared queue lists every application stopped at its final review step (or held only by a step in the browser); each one's review page shows every answer with where it came from, and approves, changes answers, prepares again and, only when the service allows it and the person confirms, submits (see [Review lane](#review-lane)).

Recommendations and tracked cards never apply by themselves. Applying always goes through the desk and its single backend duplicate check.

## Routes

| Route | Purpose |
| --- | --- |
| `/` | Live desk. Talks to the application service through `/api/imx/*`. With no service configured it says so and never simulates a result. |
| `/preview` | Fixture mode, clearly labelled. It uses an in-memory fictional candidate and fictional job sites. It makes no network calls and sends nothing. `?scenario=` selects a fixture (see below). |
| `/pipeline`, `/jobs` | Live pipeline board and jobs browser through the same gateway. Missing service routes and connection failures stay explicit. |
| `/review`, `/review/{applicationId}` | Live Prepared queue and one application's review page (approve, change answers, prepare again, submit when allowed). |
| `/preview/pipeline`, `/preview/jobs` | Labelled fixture versions with fictional records shaped like the reference workbook. |
| `/preview/review`, `/preview/review/{applicationId}` | Labelled fixture review lane: three fictional applications (prepared with a CAPTCHA pending, approved, held by a sign-in). Approve, edit and prepare again change the tab's copy only; the preview never submits. |
| `/api/imx/[...path]` | Same-origin gateway (`lib/gateway.ts`), described below. It never invents a result and never logs bodies. |

## Commands

Requires Node ≥ 20.9. Run all commands from `apps/web`. There is no root Node workspace.

```bash
npm ci
npm run dev -- --hostname 127.0.0.1 --port 4317   # any free port
npm run typecheck                                  # tsc --noEmit
npm test                                           # vitest: validation + preview state machine
npm run test:e2e                                   # playwright: builds, serves on a free port, desktop + mobile
npm run build
IMX_BACKEND_URL=http://127.0.0.1:8765 IMX_WEB_ORIGIN=http://127.0.0.1:4317 npm start -- --hostname 127.0.0.1 --port 4317
```

| Variable | Meaning |
| --- | --- |
| `IMX_BACKEND_URL` | S1 base URL, loopback, e.g. `http://127.0.0.1:8765`. Unset: every `/api/imx` call answers 503. |
| `IMX_WEB_ORIGIN` | This app's exact origin. It must equal S1's `IMX_SERVICE_ORIGIN`. Unset: derived from the request, so open the app at exactly the origin S1 expects. |
| `IMX_WEB_MAX_UPLOAD` | Resume upload limit in bytes (default 10 MiB, matching S1). |

`@playwright/test` is pinned to 1.62.0 to match the Chromium build already cached on this machine. Test artifacts, screenshots and the dev-server log go to the ignored `output/` directory.

## Service interface

`lib/service/types.ts` defines `ApplicationService`. Its presentation view models match the local service's HTTP contract; canonical application state and persistence remain in the backend. State names match `ARCHITECTURE.md` §6.

```ts
interface ApplicationService {
  mode: "live" | "preview";
  getCandidate(): Promise<CandidateView>;                    // saved profile + resumes
  uploadResume(file: File): Promise<ResumeDocumentView>;
  start(input: StartApplicationInput): Promise<ApplicationView>;   // the request authorizes submission
  status(id): Promise<ApplicationView>;                      // polled while REQUESTED…SUBMITTING
  answer(id, input: AnswerInput): Promise<ApplicationView>;  // save answers/attestations only
  resume(id): Promise<ApplicationView>;                      // continue after input, sign-in/CAPTCHA, or FAILED_RETRYABLE
  reconcile(id, input: ReconcileInput): Promise<ApplicationView>; // settle SUBMISSION_UNKNOWN; never a blind retry
  list(): Promise<ApplicationListView>;                      // every application with its preparation state (read only)
}
```

Implementations:

- `HttpApplicationService` (`lib/service/http.ts`) is used by `/`.
- `PreviewApplicationService` (`lib/service/preview.ts`) is used only by `/preview` and the unit tests.

Errors are `ServiceError` with `code` ∈ `unavailable | invalid | not_found | conflict | unknown`. Optional `fieldErrors` are keyed by form field, question or attestation id.

### Gateway transport (F2)

The browser calls same-origin `/api/imx/<path>`. The gateway forwards each call to `${IMX_BACKEND_URL}/<path>` under these rules:

- Every `POST` must carry `Origin` exactly equal to `IMX_WEB_ORIGIN`, or to the request's own origin when that is unset. A request with `Sec-Fetch-Site: cross-site` is answered 403 before anything is forwarded. The verified origin is sent upstream as `Origin`. A `GET` carrying a foreign `Origin` is refused, and no `Origin` is sent upstream for reads.
- JSON bodies must be `application/json` and at most 64 KiB (2 MiB for `pipeline/import/*`). They are forwarded byte for byte with a `Content-Length` and no chunked encoding.
- **Resume upload:** the browser sends `multipart/form-data`, field `file`. The gateway forwards the raw bytes as `application/octet-stream` with an `X-Imx-Filename` header holding the percent-encoded UTF-8 file name. Empty files are rejected with 400 and files over the limit with 413. Both carry `fieldErrors.resumeFile`.
- Query strings and `.`/`..` path segments are refused. Responses pass through with only `Content-Type`, `Content-Disposition`, `Content-Security-Policy` and `X-Content-Type-Options`, plus `Cache-Control: no-store`. Evidence downloads keep S1's disposition and sandbox headers.
- With no backend configured, or when it can't be reached, the gateway answers 503 `unavailable`. A `POST` whose connection dropped after sending says the service may still be acting on it.

### Desk routes

Personal data travels only in request bodies and never in query strings.

| Operation | Method and path | Body | Success |
| --- | --- | --- | --- |
| getCandidate | `GET /api/imx/candidate` | — | `CandidateView` |
| uploadResume | `POST /api/imx/resumes` | `multipart/form-data`, field `file` | `ResumeDocumentView` |
| start | `POST /api/imx/applications` | `StartApplicationInput` | `ApplicationView` |
| status | `GET /api/imx/applications/{id}` | — | `ApplicationView` |
| answer | `POST /api/imx/applications/{id}/answers` | `AnswerInput` | `ApplicationView` (validation problems in `needs.errors`) |
| resume | `POST /api/imx/applications/{id}/resume` | `{}` | `ApplicationView` |
| reconcile | `POST /api/imx/applications/{id}/reconcile` | `ReconcileInput` | `ApplicationView` |
| evidence | `GET /api/imx/applications/{id}/evidence/{evidenceId}` | — | The artifact bytes. `EvidenceView.href` points here. |
| list | `GET /api/imx/applications` | — | `ApplicationListView {applications}`: summaries, most recently updated first, each with `preparation` and `pipelineEntryIds` (presentation version 2) |

Error responses use `{"error": {"code", "message", "fieldErrors"?}}`. Status codes map to error codes as follows: 503/502/504 → `unavailable`, 400/422 → `invalid`, 404 → `not_found` and 409 → `conflict`.

### Expectations the UI relies on

- Report `SUBMITTING` before the irreversible action. Report `SUBMITTED` only with observed acceptance evidence in `receipt.evidence`, where `source: "site"` means observed on the site.
- **Receipt confirmation fields:** `receipt.confirmationMethod` is one of `SUBMISSION_OBSERVED`, `SITE_CONFIRMATION`, `ATS_CANDIDATE_PORTAL`, `CONFIRMATION_EMAIL` or `USER_CONFIRMED`. `receipt.confirmationAuthority` is `"site"` or `"user"`, and is `"user"` exactly when the method is `USER_CONFIRMED`, even if older site screenshots from the uncertain attempt are also listed.
- **How the desk uses them:** a user-confirmed receipt is headed "Submitted, on your report", stamped "Reported" and carries a caveat. Both fields are optional for backward compatibility. Without them, any user statement in the evidence makes the receipt user-reported (`lib/receipt.ts`).
- **Profile location:** `profile.location` is sent exactly as typed in "City and region", e.g. `Austin, TX` (city, then state/region; a country only if the user adds one). The service must map it to city and region and must not infer a country from the region.
- An ambiguous outcome is `SUBMISSION_UNKNOWN` with `uncertain` populated. `resume` refuses it with a 409. `user_confirmed_not_received` records a user report and keeps it locked; only site proof of non-submission permits another attempt. The preview follows the same rule.
- `needs.kind === "questions"` carries the site's own options verbatim. Attestations arrive with `accepted: false` unless the user already accepted them. Nothing is prefilled from unrelated data.
- `needs.kind === "interaction"` covers sign-in, CAPTCHA and verification. The user acts in the visible browser, then calls `resume`.
- **Prepared for review:** `NEEDS_INPUT` with `preparation` set (`ready: true`, `submitted: false`) means every page was filled and the run stopped at the site's final review step. Nothing was submitted. The desk heads it "Prepared for your review — nothing submitted", stamps it "Prepared" (dated `preparedAt`) and marks Filling done and Submitting not reached on the rail. It shows `preparation.evidence` (the review page screenshot, served by the evidence route), the `review` list and the final review page from `formUrl`, as scheme, host and path only (a query or fragment can hold a draft token and is never shown). `needs` is `null` unless questions remain; remaining questions are asked inside the prepared review, and an `interaction` need is never shown for it. `captchaPending` adds a note that a CAPTCHA must be solved in the browser before the application can be submitted. **Prepare again** calls `resume`, which must read and fill the form again and stop at the review step again, never submit. Without `preparation`/`review` (services before presentation version 2), or with a `preparation` that isn't exactly `ready: true` and `submitted: false`, the desk keeps the generic pause (`isPrepared` in `lib/preparation.ts`, which the pipeline uses too).
- **Review list:** each `review` item has the question (recorded wording, or a plain name with `wordingRecorded: false`, shown with a "see the screenshot" hint), a 1-based `page` (grouped under "Page N" when there is more than one page), `control`, `value` (text, option label, list of labels, "Yes"/"No" or the file name; never ids), `source` and `confidence`. Sources read "Your details", "Saved answer", "From your profile", "Your answer", "Drafted from your facts" and "Your resume". Confidence shows only below 1, and below 0.9 the answer is flagged "Check this".
- **Lookups:** a question with `lookup: true` and `options` is always a select of the site's suggestions, ending with "Enter a different value…", which opens a text box. The answer is the chosen suggestion or the typed text, which the browser types into the site's search box as written; the menu entry itself is never sent. A saved answer outside the suggestions reopens in that mode. A lookup without `options` is a text box. Any non-blank text is valid; a blank different value is refused before anything is sent.
- A `DUPLICATE` response includes `prior`, the earlier confirmed submission. Nothing is sent.
- `failure.retryable` controls whether **Try again** is offered.
- Events are shown verbatim in the docket (the event timeline).

## Pipeline, jobs and selection routes (S2/S3)

These are the contracts the Pipeline and Jobs views call today. The presentation types are in `lib/pipeline/types.ts` and `lib/jobs/types.ts`, and they mirror core D0 (`PipelineEntry`, `JobSearchQuery`, `JobListing`, `SourceSearchResult`, `SelectionPreferences`, `JobSelection`) and P1's tracker fields. All bodies are JSON, all mutations are `POST`, and errors use the structured shape above. `404` means a record is genuinely absent. A disabled route group or an unavailable service stays visibly unavailable.

### Pipeline (P1 via S1)

| Operation | Method and path | Body | Success |
| --- | --- | --- | --- |
| board | `GET /pipeline` | — | `PipelineBoardView {lanes, entries}` |
| create | `POST /pipeline/entries` | `PipelineEntryInput {lane, fields, applicationUrl, listingId?}` | 201 `PipelineEntryView` |
| update | `POST /pipeline/entries/{id}` | `PipelineUpdateInput {revision, fields?: Partial<PipelineFields>, applicationUrl?}` | `PipelineEntryView` |
| move | `POST /pipeline/entries/{id}/move` | `{revision, lane}` | `PipelineEntryView` |
| import preview | `POST /pipeline/import/preview` | `{format: "csv" \| "json", fileName, content, sourceId}` (≤ 2 MiB) | `ImportPreviewView` |
| import commit | `POST /pipeline/import/{previewId}/commit` | `{}` | `ImportReceiptView` |

Semantics the UI relies on:

- **Fields:** `PipelineFields` is the 23 reference fields in workbook order (the table below). Blank is `null`, never `0` or `""`. Dates are `YYYY-MM-DD`, `interviewTimeCT` is text in America/Chicago, and `decisionDueText` stays free text. `fitScore` is the user's 0–10 score, never Jev confidence. Compensation is USD per year and `compensationLow ≤ compensationHigh`.
- **Lanes:** `lane` is a lane `id` from `GET /pipeline`. Initial ids are `saved, applied, scheduling, interviewing, assessment, follow-up, decision, offer, closed`. `stage`/`status` stay verbatim, separately editable text. Moving a card never submits anything or changes an application state.
- **Updates:** in an update body, an omitted `fields` key or `applicationUrl` means unchanged. `applicationUrl: null` clears the link.
- **Concurrency:** `revision` increments on every write. A stale `revision` answers 409 `conflict`, and the UI reloads the entry instead of overwriting it.
- **History and provenance:** `history` records creation, import, moves and edits. `provenance.importedValues` keeps the immutable original cells. Additive `latestImportedValues`, `sourceId`, `firstImportedAt` and `versionCount` expose later imports separately. The import's editable tracker name is a stable `sourceId`, reused for revised exports of the same tracker; it is never a content digest.
- **Linked records:** `application` (a canonical application summary) is the only thing that may show a receipt or submitted state. `selection` shows a linked Jev decision. `applicationUrl` may be `null`; the UI asks for it before applying and never invents one.
- **Prepared for review:** with every board load and refresh the board also reads `GET /applications` (`ApplicationListView`). An application is prepared when it is `NEEDS_INPUT` with a `preparation` (`ready: true`, `submitted: false`): the form was filled and the run stopped at the final review step without submitting. A card linked to that application, or listed in its `pipelineEntryIds` (the service includes unlinked cards whose application link resolves to it), is marked "Prepared for review", plus "· CAPTCHA to solve" when `preparation.captchaPending`. The card's own linked application wins; a card linked to a submitted application keeps its receipt and is never marked, and a card in the Closed lane is never marked either. The mark replaces "Application: waiting for you" and is never a receipt. The "Prepared for review" focus filter counts these cards, applies to board and list layouts and combines with the text filter. If the list can't be read (an older service answering 404/405, or the service unavailable), the board works as before with no marks and one quiet note (`lib/pipeline/prepared.ts`).
- **Import:** preview validates every row and reports `errors[{field, message}]` by `rowNumber`. Commit is refused (409 or 400) while any row has errors, so there is never a partial silent import. Reimporting unchanged data reports `unchanged` and never duplicates cards or overwrites later manual edits.

### Jobs and selection (J1/J2 via S1)

| Operation | Method and path | Body | Success |
| --- | --- | --- | --- |
| preferences | `GET /selection/preferences` | — | `SearchPreferencesView` |
| save preferences | `POST /selection/preferences` | `SearchPreferencesView` without `fingerprint` | `SearchPreferencesView` |
| start search | `POST /jobs/search` | the same preferences body (the search query) | 202 `SearchRunView` |
| search status | `GET /jobs/search/{runId}` | — | `SearchRunView` (polled until `finishedAt`) |
| listings | `GET /jobs` | — | `ListingsView {listings, lastRun}` |
| listing | `GET /jobs/{listingId}` | — | `ListingView` with current decision task |
| decide | `POST /selection/jobs/{listingId}` | `{}` | 200 completed `ListingView`, or 202 accepted and still deciding |
| track | `POST /jobs/{listingId}/track` | `{}` | `ListingView` with `pipelineEntryId` |

- **Search defaults:** Paid Media Manager, Senior Paid Media Manager, Performance Marketing Manager, Growth Marketing Manager, Demand Generation Manager and Digital Marketing Manager are representative seeds, followed by the broader Marketing Manager and Marketing Director. `roleFocus` explains hands-on paid acquisition and growth ownership and is editable, persisted and sent with both saves and searches. Jev evaluates responsibilities; exact title matches are not required. Onsite/hybrid targets default to Austin, TX; remote eligibility is United States, nationwide. The floor is USD100000/year, with unstated pay kept visibly unresolved.
- **Location priority:** `locationPriority` is D0 `LocationPriority`: `STRONGLY_PREFER_ONSITE_HYBRID` (default), `BALANCED` or `PREFER_REMOTE`. It is part of the preferences body for both save and search, and of the preferences fingerprint, so changing it marks decisions stale. It only orders results; eligible remote roles are never dropped. A preferences response without the field is treated as the default.
- **Location tier:** each listing may carry an optional `locationTier`: `ONSITE_HYBRID_TARGET`, `REMOTE_ELIGIBLE`, `REMOTE_UNCONFIRMED`, `OUTSIDE_TARGET` or `UNRESOLVED`. When it's absent, the UI derives the tier from the listing's stated `workArrangement`, `location` and `remoteEligibility` (`lib/jobs/ranking.ts`). A missing location or arrangement is `UNRESOLVED`, never assumed to be Austin. Results are grouped by tier in priority order: under the default, target-city onsite/hybrid comes first, then remote open to the region, then remote with unstated eligibility, then unresolved, then elsewhere.
- **Per-source state:** each source reports one of `QUEUED`, `RUNNING`, `OK`, `PARTIAL`, `NEEDS_USER`, `BLOCKED`, `ERROR` or `SKIPPED`, with `message`, `userAction` and `sessionName`. `NEEDS_USER` and `BLOCKED` are never shown as empty success.
- **Listings:** listings carry only observed facts. `workArrangement: "UNKNOWN"`, `compensation: null` and absent fields stay visibly unknown. `status: "CLOSED"` listings are marked and never recommended. `provenance` lists every source URL and application URL seen.
- **Posting links:** `postingUrl` and `provenance[].postingUrl` are job-specific postings. `sourceUrl` may be a search page. Navigation and the Apply handoff use an actual `applicationUrl`, then `postingUrl`; neither substitutes a search URL.
- **Delayed decisions:** a 202 is pending, not a decision. The UI polls `GET /jobs/{id}` and remembers the pending task across a page reload without re-POSTing. S3R `decisionTask` has `{id,state,error,resultId,requestedAt,updatedAt}`; `DONE` finishes even for a cached unchanged selection id, while `FAILED`/`INTERRUPTED` stops with an actionable message. A fresh tab or Refresh listings recovers durable `QUEUED`/`RUNNING` tasks even without a local session marker. An older service without task state has a bounded wait and a manual Refresh listings action.
- **Decisions:** `SelectionView` separates Jev's `modelChoice`, `probabilities` and `confidence` from the `effectiveChoice` after `holds`. It also carries `reasons`, `unresolved` facts, `providerError`, the requested and returned models, `rubricVersion` and `stale`. A provider failure or missing evidence can never produce an effective `APPLY`.
- **Applying:** "Apply" from a listing or card only prefills the desk. The user's press of **Apply and submit** starts the normal `POST /applications`, where S1's duplicate check applies. The request includes optional `pipelineEntryId`/`listingId` only while the URL matches the handoff. The service validates ownership and matching URLs, then links the canonical application before execution. Changing the URL starts an independent application. The tracker reads receipt authority from that linked application.
- **Reviewing:** a prepared card offers "Review" instead of "Apply". It hands the desk the application id (`DeskHandoff.applicationId`, with the application's link); the desk reads it with `GET /applications/{id}` and follows it as it follows a started or restored application. Nothing is prefilled, started, resumed or submitted. If it can't be read, the desk says "Couldn't open the prepared application" with the reason and shows the empty compose form.

## Review lane

The Review section is where the person reviews and submits prepared applications, one at a time, from the queue. Types are in `lib/review/types.ts` (the service's "review lane" models, camelCase), the client in `lib/review/http.ts`, the pure rules in `lib/review/logic.ts` and the labelled fixtures in `lib/review/preview.ts`.

| Operation | Method and path | Body | Success |
| --- | --- | --- | --- |
| queue | `GET /api/imx/review` | — | `ReviewQueueView {applications}`: newest first by `stoppedAt` |
| review | `GET /api/imx/applications/{id}/review` | — | `ApplicationReviewView`: the desk's `application` plus `stage`, `preparedPacketId`, `approval`, `changedSincePreparation`, `providerCost`, `answers`, `editNote`, `submit`, `browser` |
| approve | `POST /api/imx/applications/{id}/approve` | `{packetId}`: the `preparedPacketId` the page showed | `ApplicationReviewView` |
| submit | `POST /api/imx/applications/{id}/submit` | `{packetId, confirm: true}`: the approval's `packetId` | `ApplicationReviewView` |
| change an answer | `POST /api/imx/applications/{id}/answers` (the desk's route) | `{answers: {questionId: value}, attestations: {}, reuse: {questionId: scope}}`, or the value under `attestations` for a statement | `ApplicationView`; a refused answer is 422 with `fieldErrors[questionId]` |
| prepare again, resume in the browser | `POST /api/imx/applications/{id}/resume` (the desk's route) | `{}` | `ApplicationView` |

Semantics the pages rely on:

- **Queue.** One row per application: employer, role, backend (`job.ats`), prepared time, AI provider cost (`providerCost`: the known USD amount, the calls, and calls without a reported cost), the service's one-line `hold.summary`, and Approved, CAPTCHA and In-the-browser marks. Filters: All, To review (prepared, not approved), Approved, In the browser. A service without the route (404) gets one quiet note; an unreachable one the usual service notice.
- **Answers.** `answers` is every question of the prepared form in form order, page by page, blanks included (`value: null`, "Left blank"). Each carries a provenance badge by `provenance.kind`: `identity`, `resume`, `saved_answer`, `saved_policy` (standing answer rules: a policy, or an answer saved for every application), `derived` (salary, work authorization status and start date derivations), `fact_screener`, `narrative` (written from facts and stories, shown in full with a Copy button), `user` and `blank`, with the service's `label` and the resolver's `detail`. `citations` (fact, story passage and job evidence ids) open in a disclosure. Confidence below 0.9 is flagged "Check this". The values are the person's own data: the page shows them, and nothing is logged.
- **Changing an answer.** Only a row with `edit` and a `questionId` offers **Edit answer**; otherwise `noEditReason` says why (for example options that weren't recorded). The editor follows `edit.control` (text, long text, a lookup's text, radios or a menu for `options`, checkboxes, Yes/No, or a statement checkbox for `edit.attestation`) and offers only the `edit.reuse` scopes: this application only, this job, or every application. **Save and prepare again** (the default) saves through the answers route and then calls `resume`; **Save only** leaves the form as it was, and the page says the answers changed since this preparation. A blank never replaces an answer. Saving withdraws an approval.
- **Approve.** Offered only for a current preparation (`preparedPacketId`), not approved, not changed since, with no required question left open. It sends that packet id, so a preparation that changed meanwhile is refused instead of approved. Approving submits nothing.
- **Submit.** The button is off, with every reason listed, unless `submit.allowed` (the service started with `IMX_ALLOW_SUBMISSION=1`, a valid approval, a submittable state, the mode rule below), and in live mode the fresh `/healthz` agrees (`submission` not `"disabled"`, runner available). When on, it opens a confirmation naming the employer, with a required "I reviewed every answer" checkbox; only then is `{packetId: approval.packetId, confirm: true}` sent. When `submit.opensBrowser`, the confirmation says a browser window opens (for a CAPTCHA or sign-in). `submit.command` shows the terminal equivalent. The page polls while the application is working and then shows the outcome, with a link to the desk for the receipt or an uncertain submission.
- **Resume in browser.** For an application held only by a sign-in, a CAPTCHA or another browser step: `resume` when `browser.available` (a visible browser window opens), else `browser.command` (`interviewmaxxing resume APP --act`) with a Copy button.
- **Mode rule.** The review lane follows the service's `applicationMode`: a `TEST_ONLY` service acts only on local test applications (loopback URLs), a `LIVE` service on the employer's site (`reviewExecutionProblem` in `lib/review/logic.ts`). The service enforces the same rule. The desk keeps its own test-only gate.

### Development readiness and visual checkpoint

`GET /healthz` reports `executor: "idle" | "busy"`, `runner: "available" | "unavailable"` and `applicationMode: "TEST_ONLY" | "LIVE"`, from the review lane `submission: "enabled" | "disabled"` (`IMX_ALLOW_SUBMISSION=1` at service start) and `browser: "visible" | "headless"`, and from presentation version 2 `presentationVersion: "2"` (absent on older services, which send no `preparation`, `review`, `lookup` or application list; the desk and board then keep their earlier behaviour). The desk and board read it (`presentationSupport` in `lib/service/readiness.ts`): for a major version they don't know, the desk drops `preparation`, `review` and `lookup` (a prepared stop shows as the generic pause) and says so in a notice, and the board marks no card and says why in one quiet line. The desk requires `TEST_ONLY`, an available runner and a loopback target; an absent health response fails closed. The same gate covers resume, answer-and-continue and site recheck. Each execution action fetches current health, so runner recovery needs no page reload. User-reported reconciliation involves no browser execution and remains available. Jobs and pipeline show the test-mode notice independently of read-only discovery. The desk intentionally does not start real employer applications in this development deployment. In `LIVE` mode the banner says the service works on real employer sites and that nothing is submitted unless submission is on and the person approves and confirms the application in Review; it shows "Submission on" or "Submission off" when the service reports it.

The workspace uses Newsreader for page titles, Schibsted Grotesk for controls/data and restrained forest accents. Jobs shows an editable search brief above result rows, with evidence and decision reasoning side by side. Pipeline includes actionable filters (next actions, upcoming interviews, prepared for review), readable dates/pay, separate arrangement/commute, and both board and list layouts. Upcoming interviews includes dates today or later in America/Chicago; historical follow-up suggestions are not appointments. Linked pipeline receipts require explicit site confirmation authority; user reports and missing authority remain labelled separately.

The default automated suite covers preview interactions, same-origin transport, recovery races, 202/cached/interrupted decision handling, source-vs-posting links and the local-test dispatch boundary. The separate fictional acceptance suite below verifies frontend → service → real runner → Chromium → localhost ATS, including server-side submission evidence. Its result is tied to the tested backend checkout, not to preview fixtures.

### Fictional local acceptance

`scripts/fictional-service.py` starts the actual service, candidate/pipeline/jobs/selection stores and I1 runner over a newly created temporary home. The application site is the backend's localhost mock ATS, with matching fictional marketing titles, company and job codes configured by `scripts/fictional_ats.py`. Its form handling, uploads, submission records and uncertain-outcome behavior are unchanged. Job discovery and Jev's HTTP transport are fixtures. The candidate starts from the backend's fictional Avery Quill test profile with fictional marketing answers; the harness never loads the default user home or real provider credentials.

Use a Python environment with the backend dependencies and a committed backend integration checkout. Keep the frontend origin and backend port aligned:

```bash
python scripts/fictional-service.py --backend-root /path/to/committed/backend \
  --output output/live-acceptance --origin http://127.0.0.1:4382 --port 4383
```

In separate terminals, build and run the frontend, then run acceptance:

```bash
npm run build
IMX_BACKEND_URL=http://127.0.0.1:4383 IMX_WEB_ORIGIN=http://127.0.0.1:4382 \
  npm start -- --hostname 127.0.0.1 --port 4382
npx playwright test --config=playwright.live.config.ts
```

Start a fresh fixture process/home for each full acceptance run. Stop and restart the production frontend after any new build so it serves the matching asset set. The suite checks real local submission counts and receipt evidence, resume hashes, paused-answer reload, duplicate protection, unknown-outcome reconciliation, Jev persistence, tracker linking and all-column import/reimport. Screenshots and traces stay under ignored `output/`.

### Reference field mapping

| Workbook header | Field |
| --- | --- |
| Company | `company` |
| Role | `role` |
| Stage | `stage` |
| Status | `status` |
| Priority | `priority` |
| Fit / 10 | `fitScore` |
| Next interview date | `nextInterviewDate` |
| Time (CT) | `interviewTimeCT` |
| Interview format | `interviewFormat` |
| Work arrangement | `workArrangement` |
| Location / commute | `locationCommute` |
| Comp low (USD/year) | `compensationLow` |
| Comp high (USD/year) | `compensationHigh` |
| Comp basis | `compensationBasis` |
| Target assessment | `targetAssessment` |
| Source / recruiter | `sourceRecruiter` |
| Follow-up date (suggested) | `suggestedFollowUpDate` |
| Next action | `nextAction` |
| Last interview date | `lastInterviewDate` |
| Decision due | `decisionDueText` |
| Comp / benefits notes | `compensationBenefitsNotes` |
| Fit rationale | `fitRationale` |
| Process / source notes | `processSourceNotes` |

## Preview scenarios

`straight`, `prepared` (fills both pages and stops at the final review step; nothing is submitted, and **Prepare again** repeats the run), `lookup` (the location question offers three matching places; pick one or enter a different value, then the form is prepared for review with a CAPTCHA still to solve), `questions`, `sign_in`, `captcha`, `duplicate`, `uncertain` (first recheck is inconclusive, second finds a portal record), `connection_drop` (status unreachable twice while submitting), `failure_retryable`, `failure_permanent`, `unavailable`.

The review lane's preview (`/preview/review`) holds `pv_prepared_northwind` (every provenance kind, a cited narrative, a blank optional field, a CAPTCHA pending), `pv_approved_larkspur` (approved) and `pv_signin_quarry` (held by a sign-in; **Resume in browser** acts as if you signed in and the form was prepared). Submit is always off there.

Every preview session also holds one seeded, already-prepared application, `pv_prepared_northwind` (Senior Lifecycle Marketer at Northwind Cartography, CAPTCHA pending). `status()` returns it and `list()` lists it with `pipelineEntryIds: ["pipe_pv_northwind"]`, followed by applications started in the session (`pipelineEntryIds: []`). It never changes a scenario's flow.

## Privacy and accessibility notes

- Forms use `method="post"` and are handled client-side, so data never lands in the URL even before hydration. The desk stores only the active application id in `sessionStorage` (live mode). On reload it restores that application (`lib/restore.ts`). A transient failure (service unreachable, 5xx, conflict) keeps the id and shows a warning that the application may still be in progress, with **Check again**. Only a definitive `404 not_found` clears it. The user can also choose to stop following it on that page.
- Every control has a label. Errors are linked from a focused summary and reported through `aria-invalid`/`aria-describedby`. Focus moves to the state headline when the application needs the user or finishes, and state changes are announced politely.
- `prefers-reduced-motion` disables all animation.

## Known limitations

- The live desk works once S1 is running and `IMX_BACKEND_URL`/`IMX_WEB_ORIGIN` are set. Pipeline and Jobs need the S1 routes above, backed by P1, J1 and J2.
- Evidence screenshots are shown from the `href` the service provides. The service must serve those artifacts.
- The review lane changes an answer only where the service offers an editor (`edit`). A choice whose options weren't recorded when the form was filled, and the pinned resume, show why they can't be changed there. Contact details can be changed for one application only; the profile is where they change everywhere.
- The review list of a prepared application shows what the service reports it entered. The desk can't compare it with the live page, so the screenshot of the review page is the reference, and the desk never solves a CAPTCHA.
- The preview's job identity comes from fixtures and does not reflect the URL you type.
