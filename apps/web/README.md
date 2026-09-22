# Interviewmaxxing web — application desk, pipeline and jobs

Next.js 16 / React 19 / TypeScript frontend for Interviewmaxxing. It has three views:

- **Desk:** the supplied-URL application flow (`ARCHITECTURE.md` §2 and §17). The user pastes an application link, confirms their details and resume, and presses **Apply and submit**. That press authorizes submission. The desk shows progress, asks only for missing required answers, statements only the user can make or a sign-in/CAPTCHA, and ends with a receipt or an accurate blocked/uncertain state.
- **Pipeline:** the user's own tracker, using the 23-column reference workbook schema.
- **Jobs:** search across job sources, with Jev APPLY/SKIP/REVIEW decisions.

Recommendations and tracked cards never apply by themselves. Applying always goes through the desk and its single backend duplicate check.

## Routes

| Route | Purpose |
| --- | --- |
| `/` | Live desk. Talks to the application service through `/api/imx/*`. With no service configured it says so and never simulates a result. |
| `/preview` | Fixture mode, clearly labelled. It uses an in-memory fictional candidate and fictional job sites. It makes no network calls and sends nothing. `?scenario=` selects a fixture (see below). |
| `/pipeline`, `/jobs` | Live pipeline board and jobs browser, through the same gateway. They show an honest "not available" state until S1 serves those routes. |
| `/preview/pipeline`, `/preview/jobs` | Labelled fixture versions with fictional records shaped like the reference workbook. |
| `/api/imx/[...path]` | Same-origin gateway (`lib/gateway.ts`), described below. It never invents a result and never logs bodies. |

## Commands

Requires Node ≥ 20.9. Run all commands from `apps/web`. There is no root Node workspace.

```bash
npm install
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

`lib/service/types.ts` defines `ApplicationService`. Its types are **presentation view models** pending alignment with the canonical core contracts (C1). They are not a second backend model. State names match `ARCHITECTURE.md` §6.

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

Error responses use `{"error": {"code", "message", "fieldErrors"?}}`. Status codes map to error codes as follows: 503/502/504 → `unavailable`, 400/422 → `invalid`, 404 → `not_found` and 409 → `conflict`.

### Expectations the UI relies on

- Report `SUBMITTING` before the irreversible action. Report `SUBMITTED` only with observed acceptance evidence in `receipt.evidence`, where `source: "site"` means observed on the site.
- An ambiguous outcome is `SUBMISSION_UNKNOWN` with `uncertain` populated. `resume` must refuse it with a 409, and only `reconcile` settles it. `user_confirmed_not_received` may move it to `FAILED_RETRYABLE`.
- `needs.kind === "questions"` carries the site's own options verbatim. Attestations arrive with `accepted: false` unless the user already accepted them. Nothing is prefilled from unrelated data.
- `needs.kind === "interaction"` covers sign-in, CAPTCHA and verification. The user acts in the visible browser, then calls `resume`.
- A `DUPLICATE` response includes `prior`, the earlier confirmed submission. Nothing is sent.
- `failure.retryable` controls whether **Try again** is offered.
- Events are shown verbatim in the docket (the event timeline).

## Proposed S1 routes: pipeline, jobs and selection (F3)

These are the contracts the Pipeline and Jobs views call today. The presentation types are in `lib/pipeline/types.ts` and `lib/jobs/types.ts`, and they mirror core D0 (`PipelineEntry`, `JobSearchQuery`, `JobListing`, `SourceSearchResult`, `SelectionPreferences`, `JobSelection`) and P1's tracker fields. All bodies are JSON, all mutations are `POST`, and errors use the structured shape above. `404` means a record is genuinely absent. Until S1 serves a route group, the view shows it as not available.

### Pipeline (P1 via S1)

| Operation | Method and path | Body | Success |
| --- | --- | --- | --- |
| board | `GET /pipeline` | — | `PipelineBoardView {lanes, entries}` |
| create | `POST /pipeline/entries` | `PipelineEntryInput {lane, fields, applicationUrl, listingId?}` | 201 `PipelineEntryView` |
| update | `POST /pipeline/entries/{id}` | `PipelineUpdateInput {revision, fields?: Partial<PipelineFields>, applicationUrl?}` | `PipelineEntryView` |
| move | `POST /pipeline/entries/{id}/move` | `{revision, lane}` | `PipelineEntryView` |
| import preview | `POST /pipeline/import/preview` | `{format: "csv" \| "json", fileName, content}` (≤ 2 MiB) | `ImportPreviewView` |
| import commit | `POST /pipeline/import/{previewId}/commit` | `{}` | `ImportReceiptView` |

Semantics the UI relies on:

- **Fields:** `PipelineFields` is the 23 reference fields in workbook order (the table below). Blank is `null`, never `0` or `""`. Dates are `YYYY-MM-DD`, `interviewTimeCT` is text in America/Chicago, and `decisionDueText` stays free text. `fitScore` is the user's 0–10 score, never Jev confidence. Compensation is USD per year and `compensationLow ≤ compensationHigh`.
- **Lanes:** `lane` is a lane `id` from `lanes`, the user's tracking category. The proposed initial lanes are `saved, applied, scheduling, interviewing, assessment, follow_up, decision, offer, closed`. `stage`/`status` stay verbatim, separately editable text. Moving a card never submits anything or changes an application state.
- **Concurrency:** `revision` increments on every write. A stale `revision` answers 409 `conflict`, and the UI reloads the entry instead of overwriting it.
- **History and provenance:** `history` records creation, import, moves and edits. `provenance.importedValues` keeps the original cell text by header, plus the source digest, row and import time.
- **Linked records:** `application` (a canonical application summary) is the only thing that may show a receipt or submitted state. `selection` shows a linked Jev decision. `applicationUrl` may be `null`; the UI asks for it before applying and never invents one.
- **Import:** preview validates every row and reports `errors[{field, message}]` by `rowNumber`. Commit is refused (409 or 400) while any row has errors, so there is never a partial silent import. Reimporting unchanged data reports `unchanged` and never duplicates cards or overwrites later manual edits.

### Jobs and selection (J1/J2 via S1)

| Operation | Method and path | Body | Success |
| --- | --- | --- | --- |
| preferences | `GET /selection/preferences` | — | `SearchPreferencesView` |
| save preferences | `POST /selection/preferences` | `SearchPreferencesView` without `fingerprint` | `SearchPreferencesView` |
| start search | `POST /jobs/search` | the same preferences body (the search query) | 202 `SearchRunView` |
| search status | `GET /jobs/search/{runId}` | — | `SearchRunView` (polled until `finishedAt`) |
| listings | `GET /jobs` | — | `ListingsView {listings, lastRun}` |
| decide | `POST /selection/jobs/{listingId}` | `{}` | `ListingView` with `selection` |
| track | `POST /jobs/{listingId}/track` | `{}` | `ListingView` with `pipelineEntryId` |

- **Search defaults:** the defaults (see `DEFAULT_PREFERENCES`) are title phrases "marketing manager" and "marketing director", onsite `Austin, TX` with `ONSITE`/`HYBRID`, and remote `{eligibleRegion: "United States"}`, which is nationwide, not Texas-only. The pay floor is `{amount: 100000, currency: "USD", period: "YEAR"}` with unknown pay kept (`unknownCompensation: "KEEP"`).
- **Per-source state:** each source reports one of `QUEUED`, `RUNNING`, `OK`, `PARTIAL`, `NEEDS_USER`, `BLOCKED`, `ERROR` or `SKIPPED`, with `message`, `userAction` and `sessionName`. `NEEDS_USER` and `BLOCKED` are never shown as empty success.
- **Listings:** listings carry only observed facts. `workArrangement: "UNKNOWN"`, `compensation: null` and absent fields stay visibly unknown. `status: "CLOSED"` listings are marked and never recommended. `provenance` lists every source URL and application URL seen.
- **Decisions:** `SelectionView` separates Jev's `modelChoice`, `probabilities` and `confidence` from the `effectiveChoice` after `holds`. It also carries `reasons`, `unresolved` facts, `providerError`, the requested and returned models, `rubricVersion` and `stale`. A provider failure or missing evidence can never produce an effective `APPLY`.
- **Applying:** "Apply" from a listing or card only prefills the desk. The user's press of **Apply and submit** starts the normal `POST /applications`, where S1's duplicate check applies.

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

`straight`, `questions`, `sign_in`, `captcha`, `duplicate`, `uncertain` (first recheck is inconclusive, second finds a portal record), `connection_drop` (status unreachable twice while submitting), `failure_retryable`, `failure_permanent`, `unavailable`.

## Privacy and accessibility notes

- Forms use `method="post"` and are handled client-side, so data never lands in the URL even before hydration. The desk stores only the active application id in `sessionStorage` (live mode). On reload it restores that application (`lib/restore.ts`). A transient failure (service unreachable, 5xx, conflict) keeps the id and shows a warning that the application may still be in progress, with **Check again**. Only a definitive `404 not_found` clears it. The user can also choose to stop following it on that page.
- Every control has a label. Errors are linked from a focused summary and reported through `aria-invalid`/`aria-describedby`. Focus moves to the state headline when the application needs the user or finishes, and state changes are announced politely.
- `prefers-reduced-motion` disables all animation.

## Known limitations

- The live desk works once S1 is running and `IMX_BACKEND_URL`/`IMX_WEB_ORIGIN` are set. Pipeline and Jobs need the S1 routes above, backed by P1, J1 and J2.
- Evidence screenshots are shown from the `href` the service provides. The service must serve those artifacts.
- The preview's job identity comes from fixtures and does not reflect the URL you type.
