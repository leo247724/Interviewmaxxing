# interviewmaxxing-service (S1)

Loopback-only HTTP service between the frontend's same-origin gateway (`apps/web`, F2) and the local executor. It maps canonical store state to the frontend's presentation models (`apps/web/lib/service/types.ts`) and hands all execution to the I1 runner. It is not a second executor or state machine. The canonical `ApplicationStore` is the only state authority.

Owner: queue-runtime. Package `interviewmaxxing-service`, import `interviewmaxxing_service`, script `interviewmaxxing-service`. Standard library HTTP only; no hosted platform, queue or extra dependency.

**Status: checkpoint.** The HTTP boundary, view mapping, answer validation, background dispatch and recovery are implemented and tested against the real store. A scripted runner performs the runner recipe's store operations. The candidate side runs against the real **C2P** `LocalCandidateStore` (candidate-brain `4d0d421`). The remaining dependency is the **I1** reusable runner: final acceptance against the real runner, Chromium and the localhost mock ATS is still to run. `integration.py` is the only module that names either API. See [Integration needs](#integration-needs).

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

**C2P (candidate-brain `4d0d421`): integrated.** `tests/service/test_candidate_integration.py` runs the service over the real `LocalCandidateStore`. It covers upload before a profile exists, private permissions, setup and reload, preserved facts/answers/resume on update, resume selection, a reused answer written to `answers.json`, an unreadable profile left untouched and the shared upload bound. It is skipped when the package is not installed.

**Dependencies.** `pyproject.toml` pins `interviewmaxxing-core==0.1.0`, `interviewmaxxing-candidate==0.1.0` and `interviewmaxxing-cli==0.1.0` (workspace sources). No third-party dependency beyond `pydantic` (already in core). The root lock needs regenerating by core/coordinator to include this member.

## Tests

```bash
uv venv .venv-task --python 3.12
uv pip install --python .venv-task/bin/python -e packages/core -e apps/cli pytest ruff mypy
uv pip install --python .venv-task/bin/python --no-deps --no-sources -e apps/service
# until the coordinator merges C2P here, install it read-only from its worktree:
uv pip install --python .venv-task/bin/python --no-deps --no-sources ../candidate-brain/packages/candidate
.venv-task/bin/python -m pytest tests/service
.venv-task/bin/ruff check apps/service tests/service && .venv-task/bin/mypy --strict apps/service/src
```

`tests/service` starts the real server on an ephemeral loopback port over a real SQLite store in a temporary `IMX_HOME`. It uses a scripted runner that performs the runner recipe's store operations against a fictional site, with either an in-memory candidate gateway or the real C2P store. Everything is fictional and local.
