# interviewmaxxing-service (S1 + S2)

Loopback-only HTTP service between the frontend's same-origin gateway (`apps/web`, F2) and the local executor. It maps canonical store state to the frontend's presentation models (`apps/web/lib/service/types.ts`) and hands all execution to the I1 runner. It is not a second executor or state machine. The canonical `ApplicationStore` is the only state authority.

Owner: queue-runtime. Package `interviewmaxxing-service`, import `interviewmaxxing_service`, script `interviewmaxxing-service`. Standard library HTTP only; no hosted platform, queue or extra dependency.

**Status: checkpoint.**
- **S1 (applications).** The HTTP boundary, view mapping, answer validation, background dispatch and recovery are tested against the real store with a scripted runner. The candidate side runs on the real C2P `LocalCandidateStore`.
- **S2 (pipeline, jobs, preferences, Jev selection).** Pipeline routes run on the real P1 `PipelineStore`. Jobs and selection run on the real J1 `JobStore`/`JobSearchService` and J2 `SelectionService`, with fixture source adapters and a fixture Jev transport in tests.
- **Still to do.** Final acceptance against the real I1 runner, Chromium, the localhost mock ATS and the UI. Until I1 is installed, application routes answer `503` and record nothing.

`integration.py` is the only module that names the C2P, J1, J2 and I1 APIs. See [Integration needs](#integration-needs).

## Run

```bash
IMX_HOME=$PWD/.imx IMX_SERVICE_ORIGIN=http://127.0.0.1:4317 \
  uv run interviewmaxxing-service --port 8765
# -> interviewmaxxing-service listening on http://127.0.0.1:8765
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `IMX_SERVICE_ORIGIN` | required | Exact frontend origin allowed to send mutations (loopback only) |
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

- `SearchPreferencesView.locationPriority` (+) takes `"STRONGLY_PREFER_ONSITE_HYBRID" | "BALANCED" | "PREFER_REMOTE"`. It is the canonical D0 `LocationPriority` and defaults to strongly preferring onsite/hybrid.
  - In request bodies it is optional. Leaving it out keeps the saved value.
  - It is part of `SelectionPreferences`, so it changes `fingerprint` (earlier decisions become `stale`), reaches Jev and J2's decision cache, and sets the J1 search leg order and budget.
- `ListingView.locationTier` (+) takes `"PREFERRED" | "EQUAL" | "SECONDARY" | "UNRANKED" | null`, and `ListingView.rankReason` (+) is a string or `null`.
  - Both come from J2's `check_location` + `location_tier`.
  - They are `null` when the selection package isn't installed.
- **Listing order:**
  1. Closed listings last.
  2. Then location tier. With the default priority, Austin onsite/hybrid is `PREFERRED` and eligible US-wide remote is `SECONDARY`, so remote roles are still listed, never excluded.
  3. Then stated pay against the floor: meets, unknown, below. Unknown pay is not demoted below pay that misses the floor.
  4. Then most recently observed.
- `PipelineEntryView.fields` holds the 23 P1 reference keys, and blank is `null`.
  - `origin` is `import` when P1 has provenance, `jobs` when linked to a listing, and `manual` otherwise.
  - `history` maps P1 kinds `created`, `imported`, `moved` and `stage_edited`; `stage_edited` becomes `edited`, with a readable summary.
  - `provenance.importedValues` is P1's `original`, keyed by workbook header. `provenance.importId` is the receipt id of that document, and `sourceDigest` is the workbook digest (else the document digest).
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
  - The call waits up to 20 s for the decision (the gateway allows 30 s). `202` means it is still running; poll `GET /jobs/{id}`.
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

Requested signatures. `integration.py` adapts to small differences without any change to the core schemas.

**I1 (core-contracts): reusable runner.**

```python
# interviewmaxxing_cli.runner
def create_runner(paths: LocalPaths, *, headless: bool, interaction: UserInteraction) -> Runner
class Runner:
    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome
    async def resume(self, application_id: str) -> ApplyOutcome
    async def reconcile(self, application_id: str) -> ApplyOutcome
```

- Called on the service's executor thread: one runner per run, one run at a time. The runner opens and closes its own `ApplicationStore` inside the call.
- `apply` must proceed for an application the service already recorded (disposition `RESUMABLE`, state `REQUESTED`). Re-recording the request is fine; the view folds that event.
- `resume` must accept `NEEDS_INPUT` and `FAILED_RETRYABLE`, plus `INSPECTING`/`PACKET_READY`/`FILLING` whose claim lapsed. For `REQUESTED` the service calls `apply`.
- With a noninteractive `UserInteraction`, `request_inputs` returns `[]`: record `NEEDS_INPUT` with the packet's missing inputs and return. `request_action` returns `False` for runs the user did not start with Continue. On a sign-in/CAPTCHA page, transition to `NEEDS_INPUT` with `metadata={"page_kind": "SIGN_IN_REQUIRED" | "CAPTCHA", "observed_url": <url>}`, which the view shows as an interaction. When it returns `True`, wait for the user in the visible browser.
- `reconcile(application_id)` re-inspects the site for this application in the browser and calls `store.reconcile_submission` only with concrete proof (ACCEPTED signals or definite NOT_SUBMITTED). It never submits. It returns the stored state otherwise.
- Release the claim before returning. Record evidence under `<artifacts>/<application_id>/` with `EvidenceRef.path`.

**I1 (current `LocalApplicationRunner`).** `runner_factory` uses `create_runner(...)` when it is published, else `LocalApplicationRunner(paths=, interaction=, headless=)`. `runner_problem()` makes application start, resume and recheck answer `503` without recording anything while the runner is not installed. Pending I1 seam: **per-application resume pinning**. Application A's selected resume must stay pinned across profile changes made for B and across a restart. When core publishes where that pin lives (on the request or application record, or a runner argument), the service will pass `StartApplicationInput.resumeId` for the new application. It will never let the runner reread the global profile's current resume.

**J1 (job-ingestion, working tree read at this checkpoint).** Uses `JobStore(path)`, `.get_listing`, `.list_listings(limit=)`, `JobSearchService(store, transport, adapters=).run(query, detail_limit=)` with a single-source query per call for per-source progress, `OpenCliTransport()` and `default_db_path()`. Requested seam, optional: `run(query, *, run_id=None, on_source=callback)`. That would let one J1 run carry the service task id instead of one J1 run per source.

**J2 (jev-selection, working tree read at this checkpoint).** Uses `SelectionService(client=, store=, application_lookup=, candidate_id=)`, `.select`, `.is_current`, `SelectionStore(default_store_path(paths))`, `.latest`, `.get`, `CandidateEvidence.from_profile`, `JevClient(load_api_key())`, `check_location`, `location_tier` and `location_priority_reason`.

**P1 (application-packets `da97188`, merged).** Uses `PipelineStore.from_paths` and `list_items`, `create_item`, `update_item`, `move_item`, `history`, `lanes`, `list_imports`, `preview_import` and `apply_import`, plus `parse_import`, `TrackingFields` and `REFERENCE_FIELDS`. P1R (source-scoped imports, immutable originals) is consumed at its checkpoint without service changes, unless its import identity API changes.

**C2P (candidate-brain `4d0d421`): integrated.** `tests/service/test_candidate_integration.py` runs the service over the real `LocalCandidateStore`. It covers upload before a profile exists, private permissions, setup and reload, preserved facts/answers/resume on update, resume selection, a reused answer written to `answers.json`, an unreadable profile left untouched and the shared upload bound. It is skipped when the package is not installed.

**Dependencies.** `pyproject.toml` pins `interviewmaxxing-core==0.1.0`, `interviewmaxxing-candidate==0.1.0`, `interviewmaxxing-pipeline==0.1.0` and `interviewmaxxing-cli==0.1.0` (workspace sources). J1 (`interviewmaxxing-jobs`) and J2 (`interviewmaxxing-selection`) are imported dynamically and are optional until they land; add them as pinned dependencies once they are workspace members. No third-party dependency beyond `pydantic` (already in core). The root lock needs regenerating by core/coordinator to include this member.

## Tests

```bash
uv venv .venv-task --python 3.12
uv pip install --python .venv-task/bin/python --no-sources -e packages/core -e packages/candidate \
    -e packages/pipeline -e packages/generation -e apps/cli pytest ruff mypy
uv pip install --python .venv-task/bin/python --no-deps --no-sources -e apps/service
# optional until J1/J2 are merged: install their current trees read-only (non-editable copies)
uv pip install --python .venv-task/bin/python --no-deps --no-sources \
    ../job-ingestion/packages/jobs ../jev-selection/packages/selection
.venv-task/bin/python -m pytest tests/service
.venv-task/bin/ruff check apps/service tests/service && .venv-task/bin/mypy --strict apps/service/src
```

`tests/service` starts the real server on an ephemeral loopback port over real SQLite stores in a temporary `IMX_HOME`:

- `test_http_flow`, `test_http_boundary`, `test_mapping`: the S1 application routes, with a scripted runner that performs the runner recipe's store operations against a fictional site.
- `test_executor`: the executor waits for a cancelled run's awaited cleanup before stopping.
- `test_candidate_integration`: the real C2P store.
- `test_pipeline_routes`: the real P1 store, including CSV import preview, commit and reimport.
- `test_jobs_routes`: the jobs/selection orchestration over protocol fakes that produce canonical D0 records.
- `test_package_integration`: the real J1 store and search service with fixture source adapters, and the real J2 selection service with a fixture Jev transport. It is skipped when J1/J2 are not installed.

Everything is fictional and local. No live source is browsed, no Jev credit is spent, and nothing is submitted.
