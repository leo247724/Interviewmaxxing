# Interviewmaxxing web — application desk

Next.js 16 / React 19 / TypeScript frontend for the supplied-URL flow in `ARCHITECTURE.md` §2 and §17. The user pastes an application link, confirms their details and resume, and presses **Apply and submit**. That press authorizes submission. The desk then shows progress, asks only for missing required answers, statements only the user can make or a sign-in/CAPTCHA, and ends with a receipt or an accurate blocked/uncertain state.

Job discovery, selection, ranking and outcome analytics are out of scope.

## Routes

| Route | Purpose |
| --- | --- |
| `/` | Live desk. Talks to the application service through `/api/imx/*`. With no service configured it says so and never simulates a result. |
| `/preview` | Fixture mode, clearly labelled. It uses an in-memory fictional candidate and fictional job sites. It makes no network calls and sends nothing. `?scenario=` selects a fixture (see below). |
| `/api/imx/[...path]` | Same-origin gateway. It forwards `GET`/`POST` to `IMX_BACKEND_URL` or answers `503 {error:{code:"unavailable"}}` when that is unset or unreachable. It never logs request bodies. |

## Commands

Requires Node ≥ 20.9. Run all commands from `apps/web`. There is no root Node workspace.

```bash
npm install
npm run dev -- --hostname 127.0.0.1 --port 4317   # any free port
npm run typecheck                                  # tsc --noEmit
npm test                                           # vitest: validation + preview state machine
npm run test:e2e                                   # playwright: builds, serves on a free port, desktop + mobile
npm run build
IMX_BACKEND_URL=http://127.0.0.1:8765 npm start    # live desk against a local service
```

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

### HTTP binding (for F2)

The browser calls these same-origin paths. The gateway forwards each one to `${IMX_BACKEND_URL}/<path>`. Personal data travels only in request bodies and never in query strings.

| Operation | Method and path | Body | Success |
| --- | --- | --- | --- |
| getCandidate | `GET /api/imx/candidate` | — | `CandidateView` |
| uploadResume | `POST /api/imx/resumes` | `multipart/form-data`, field `file` | `ResumeDocumentView` |
| start | `POST /api/imx/applications` | `StartApplicationInput` | `ApplicationView` |
| status | `GET /api/imx/applications/{id}` | — | `ApplicationView` |
| answer | `POST /api/imx/applications/{id}/answers` | `AnswerInput` | `ApplicationView` (validation problems in `needs.errors`) |
| resume | `POST /api/imx/applications/{id}/resume` | `{}` | `ApplicationView` |
| reconcile | `POST /api/imx/applications/{id}/reconcile` | `ReconcileInput` | `ApplicationView` |

Error responses use `{"error": {"code", "message", "fieldErrors"?}}`. Status codes map to error codes as follows: 503/502/504 → `unavailable`, 400/422 → `invalid`, 404 → `not_found` and 409 → `conflict`.

### Expectations the UI relies on

- Report `SUBMITTING` before the irreversible action. Report `SUBMITTED` only with observed acceptance evidence in `receipt.evidence`, where `source: "site"` means observed on the site.
- An ambiguous outcome is `SUBMISSION_UNKNOWN` with `uncertain` populated. `resume` must refuse it with a 409, and only `reconcile` settles it. `user_confirmed_not_received` may move it to `FAILED_RETRYABLE`.
- `needs.kind === "questions"` carries the site's own options verbatim. Attestations arrive with `accepted: false` unless the user already accepted them. Nothing is prefilled from unrelated data.
- `needs.kind === "interaction"` covers sign-in, CAPTCHA and verification. The user acts in the visible browser, then calls `resume`.
- A `DUPLICATE` response includes `prior`, the earlier confirmed submission. Nothing is sent.
- `failure.retryable` controls whether **Try again** is offered.
- Events are shown verbatim in the docket (the event timeline).

## Preview scenarios

`straight`, `questions`, `sign_in`, `captcha`, `duplicate`, `uncertain` (first recheck is inconclusive, second finds a portal record), `connection_drop` (status unreachable twice while submitting), `failure_retryable`, `failure_permanent`, `unavailable`.

## Privacy and accessibility notes

- Forms use `method="post"` and are handled client-side, so data never lands in the URL even before hydration. The desk stores only the active application id in `sessionStorage` (live mode). On reload it restores that application (`lib/restore.ts`). A transient failure (service unreachable, 5xx, conflict) keeps the id and shows a warning that the application may still be in progress, with **Check again**. Only a definitive `404 not_found` clears it. The user can also choose to stop following it on that page.
- Every control has a label. Errors are linked from a focused summary and reported through `aria-invalid`/`aria-describedby`. Focus moves to the state headline when the application needs the user or finishes, and state changes are announced politely.
- `prefers-reduced-motion` disables all animation.

## Known limitations

- No live backend exists yet. F2 connects `IMX_BACKEND_URL`, or replaces the gateway, once the C1 contracts are approved.
- Evidence screenshots are shown from the `href` the service provides. The service must serve those artifacts.
- The preview's job identity comes from fixtures and does not reflect the URL you type.
