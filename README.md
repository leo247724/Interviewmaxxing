# Interviewmaxxing

A local job-search workspace with a jobs browser, pipeline tracker and application desk. See [ARCHITECTURE.md](ARCHITECTURE.md) for the product design and [INTEGRATION_STATUS.md](INTEGRATION_STATUS.md) for exact build checkpoints.

- **Jobs:** OpenCLI reads LinkedIn, Built In, Indeed and Google job results. Jev through OpenRouter evaluates semantic performance-marketing fit, prioritizing Austin onsite/hybrid roles over eligible US-wide remote work and retaining unresolved compensation or location evidence for review.
- **Pipeline:** track interviews, follow-ups and decisions; edit all 23 reference-workbook fields; import CSV/JSON without duplicating prior rows; retain original source records and link application receipts.
- **Desk:** provide a URL, confirmed details and a selected resume. The Python runner fills accessible HTML forms, asks for missing information and stops at the final review step without submitting. An application you review and approve is submitted only by an explicit CLI command, exactly as approved, once; the observed confirmation is recorded and uncertain submissions remain locked for reconciliation.

The dashboard currently runs in **TEST_ONLY** mode. Its application flows are verified with fictional profiles and a localhost applicant-tracking site. Real job discovery and Jev decisions are separate from application execution. No employer acceptance or thousands-of-applications-per-day capacity is claimed.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 automatically from `.python-version`)
- Chromium for Playwright: `uv run playwright install chromium` (once)
- Node.js 20.9 or newer for the dashboard
- Installed OpenCLI and a connected Chrome Browser Bridge for live job discovery

## Install

```bash
uv sync --locked --all-packages
uv run playwright install chromium
uv run interviewmaxxing --help
```

## Run the dashboard

Start the local service from the repository root:

```bash
IMX_SERVICE_ORIGIN=http://127.0.0.1:4317 \
IMX_SERVICE_APPLICATION_MODE=TEST_ONLY \
IMX_OPENROUTER_ENV_FILE="$PWD/env.local" \
  uv run --no-sync interviewmaxxing-service --port 8765
```

The service uses `~/.interviewmaxxing` by default. To exercise applications, use a separate fictional home by setting `IMX_HOME=/path/to/fictional-home`; keep personal profiles out of test runs. The ignored OpenRouter env file is optional: without a configured key, Jev reports a provider hold. Credentials stay in the Python service.

In another terminal:

```bash
cd apps/web
npm ci
npm run build
IMX_BACKEND_URL=http://127.0.0.1:8765 \
IMX_WEB_ORIGIN=http://127.0.0.1:4317 \
  npm start -- --hostname 127.0.0.1 --port 4317
```

Open [Jobs](http://127.0.0.1:4317/jobs), [Pipeline](http://127.0.0.1:4317/pipeline) or [Desk](http://127.0.0.1:4317/). Both processes must use the same frontend origin. Search results and recommendations never submit applications automatically. Labelled fixture previews are available at `/preview`, `/preview/jobs` and `/preview/pipeline`.

The [service guide](apps/service/README.md) documents configuration, local control boundaries and recovery. The [frontend guide](apps/web/README.md) documents its routes and test harnesses. Email/calendar connections remain a [design](docs/integrations/README.md); tailored documents and cover letters have an [offline factual prototype](docs/documents/README.md). The [performance report](docs/performance/benchmarks.md) distinguishes local measurements from modeled capacity.

## Set up your profile

Your profile lives in `$IMX_HOME/profile/<candidate id>/` (default candidate id `default`):

```
profile.json   your verified contact details, facts and saved answers (CandidateProfile JSON)
answers.json   answers you chose to reuse (written by the tool; optional)
resume.pdf     the resume to upload (profile.json's resume.path may point anywhere)
```

Start from `examples/candidate.example.json` and `examples/answers.example.json` (fictional) and see `packages/candidate/README.md` for the exact rules. In short:
- Every fact states whether you verified it; unverified facts are never used in answers.
- Saved answers carry an explicit scope, either `GLOBAL` or one `JOB`, and apply only to the exact question wording they were given for.
- Work authorization, sponsorship, salary, consent, attestations and demographic questions are answered only from your saved answers or from what you type when asked; they are never inferred.

Each application keeps the resume it started with. Changing the profile's resume later affects new applications only; if an application's own resume file disappears, it stops rather than upload another one.

## Apply

```bash
interviewmaxxing apply https://jobs.example.com/acme/123      # visible browser
interviewmaxxing apply URL --headless --json                  # hidden browser, machine-readable
```

`apply` and `resume` prepare: they fill the form and stop at its final review step as `NEEDS_INPUT` (exit 3) without submitting, or earlier when something is missing. Submitting is a separate, explicit step.

### Review, approve and submit

```bash
interviewmaxxing apply URL --headless                     # prepare; stops at the final review step
interviewmaxxing status APP                               # "prepared: ... nothing was submitted"
interviewmaxxing approve APP                              # approve the prepared answers (lists them); submits nothing
IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit APP --yes  # submit exactly what you approved, once
interviewmaxxing receipt APP
```

Nothing is submitted without all three: your approval, `IMX_ALLOW_SUBMISSION=1` and `--yes`. The submission run opens the site again, checks that every question still matches what you approved (wording, options, required flag, the final step), fills the approved answers without resolving anything again and submits once. If the form changed, or the site rejects an approved answer, it stops before anything is accepted and withdraws the approval: prepare it again (`resume APP`), review and approve again. See [docs/submission.md](docs/submission.md). `submit` ends in one of these states:

| Result | Exit | What it means / what to do |
| --- | --- | --- |
| `SUBMITTED` | 0 | The site confirmed the application; the receipt is saved (`interviewmaxxing receipt APP`). |
| `NEEDS_INPUT` | 3 | The form no longer matches what you approved, or the site rejected an approved answer: nothing was submitted and the approval is withdrawn (prepare with `resume APP`, review, approve again). Or an action in the browser (sign-in, CAPTCHA, a custom control you set yourself): the approval stands; `submit APP --yes --act` in a visible browser. |
| `FAILED_RETRYABLE` | 3 | Stopped safely before submitting (browser error, a field that could not be operated, an ambiguous Next button). The approval stands; `submit` again retries. |
| `SUBMISSION_UNKNOWN` | 5 | The submit may have reached the employer but no confirmation tied to this job was seen. It is never retried; `reconcile` it. |
| not approved / submission not enabled / already submitted / duplicate / in progress | 4 | Nothing was done. Without `IMX_ALLOW_SUBMISSION=1` the command prints how to enable it. A submit interrupted by a crash becomes `SUBMISSION_UNKNOWN` once its lease lapses (ten minutes at most); `reconcile` it. It is never repeated. |

### Many jobs at once

`interviewmaxxing prepare-batch --inventory FILE --workers 3` prepares every Saved job of an inventory file through the same `apply` flow, one headless browser per worker, and stops each one at its final review step without submitting. Finished jobs are written to a resumable ledger under `$IMX_HOME/batches/`, with a summary of what is prepared and what still needs you. See [docs/mass-preparation.md](docs/mass-preparation.md). After you approve prepared applications, `IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit-approved --batch BATCH_ID --slots 3 --yes` (or `--all-approved`) submits each approved one exactly as approved and records the outcome and receipt id in the batch ledger; `batch-report BATCH_ID` then includes a Submissions table. The measured state of this path on real Saved applications, and the bottlenecks that still limit scale, are in [docs/mass-apply-readiness.md](docs/mass-apply-readiness.md).

### Missing answers (across restarts)

```bash
interviewmaxxing status APP                          # shows the exact recorded questions and options
interviewmaxxing answer APP --set notice_period="2 weeks" --set salary_expectation=150000
interviewmaxxing answer APP --answers answers.json   # {"field_id": "value", ...}
interviewmaxxing resume APP
```

Choices can be given by option label or value; several choices are separated by `;`; checkboxes take `yes` or `no`. Answers stay with that application unless you pass `--reuse job` or `--reuse global`. Questions are recorded durably, so `answer` and `resume` work in later sessions. If a question changes on the site, it is asked again. If the site rejects an answer, that question is asked again, also after a restart; an answer the site rejects a second time is asked again too, never resubmitted unchanged.

Instead of stopping, `apply --interactive` (or `resume --interactive`) asks on the terminal. For sign-in, CAPTCHA or custom controls, run `resume APP --act` without `--headless` (the two cannot be combined), complete the step in the browser window, and the run continues; if you have not finished when the wait ends, the run stops as `NEEDS_INPUT` and `resume --act` picks it up again. Take your time: the run keeps its hold on the application while it waits for you.

### Receipts and uncertain outcomes

`receipt APP` shows the confirmation reference, the confirmation signals (for example, the job id shown with "Application submitted"), timestamps and evidence files under `$IMX_HOME/artifacts/APP/`. An application is `SUBMITTED` only when the site's own confirmation names this job; a click, a redirect, a generic "Thank you!" or a timeout is not enough.

`reconcile APP` re-reads the site's public pages (confirmation or application-status page) for a `SUBMISSION_UNKNOWN` application and never resubmits. It records `SUBMITTED` only on a confirmation tied to the job; otherwise the application stays unknown. There is no command that turns your own report into a receipt or into permission to apply again.

### Supported forms and limits

- Native, accessible HTML forms: text, email, phone, URL, textarea, select, radio, checkbox, checkbox group, multiselect, resume upload and multistep forms with an unambiguous Next/Submit control.
- Custom widgets (ARIA comboboxes, typeaheads), sign-in and CAPTCHA are left to you in the visible browser.
- No site-specific adapters yet (Greenhouse, Lever, Workday, ...): the generic runtime handles them only as far as their pages are native and accessible.
- Reconciliation needs a status or confirmation page reachable from the URL you applied with. If you applied with a bare application-form URL that links nowhere, it stays unknown; check with the employer.

## Commands

| Command | Purpose |
| --- | --- |
| `interviewmaxxing apply URL [--candidate ID] [--headless] [--interactive] [--act] [--json]` | Apply to the job at `URL` |
| `interviewmaxxing resume APP [--headless] [--interactive] [--act] [--json]` | Continue a stopped application |
| `interviewmaxxing prepare-batch --inventory FILE [--backends A,B] [--limit N] [--workers N] [--max-prepared N] [--batch-id ID] [--json]` | Prepare many Saved jobs to their final review step, never submitting ([docs/mass-preparation.md](docs/mass-preparation.md)) |
| `interviewmaxxing batch-report [BATCH_ID] [--top N] [--json]` | Summarize batch ledgers: outcomes, holds, durations, cards and submissions |
| `interviewmaxxing approve APP [--packet PKT] [--json]` | Approve the prepared answers you reviewed; submits nothing ([docs/submission.md](docs/submission.md)) |
| `IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit APP --yes [--headless] [--act] [--json]` | Submit exactly what you approved, once |
| `IMX_ALLOW_SUBMISSION=1 interviewmaxxing submit-approved (--batch ID \| --all-approved) [--slots N] --yes [--batch-id ID] [--json]` | Submit every approved application of a batch (or all of them) |
| `interviewmaxxing answer APP (--set FIELD=VALUE ... \| --answers FILE) [--reuse application\|job\|global]` | Answer recorded questions |
| `interviewmaxxing reconcile APP [--headless] [--json]` | Re-check an uncertain submission on the site |
| `interviewmaxxing status [APP] [--json]` | List applications, or show one with attempts and pending questions |
| `interviewmaxxing events APP [--json]` | Event history |
| `interviewmaxxing receipt APP [--json]` | Receipt of a confirmed submission |
| `interviewmaxxing paths [--json]` | Where local data lives |

Exit status: `0` submitted/ok, `1` error, `2` usage (also `--act` with `--headless`, or `--interactive` without a terminal), `3` not submitted (input needed or stopped), `4` blocked by stored state, `5` submission uncertain, `130` interrupted. Ctrl-C or SIGTERM during a submit records `SUBMISSION_UNKNOWN`; during a question or a browser wait it stops at once with nothing submitted. A browser that cannot start is reported as a retryable stop, not an error trace.

## Local data

Everything personal stays on your machine under `IMX_HOME` (default `~/.interviewmaxxing`) and out of source control:

```
$IMX_HOME/profile/             your profile, resume and saved answers
$IMX_HOME/state/imx.sqlite3    application requests, states and events
$IMX_HOME/artifacts/<app-id>/  confirmation screenshots and other evidence
$IMX_HOME/browser/             persistent browser profile (sign-ins)
```

Each location can be overridden (`IMX_PROFILE_DIR`, `IMX_STATE_DB`, `IMX_ARTIFACTS_DIR`, `IMX_BROWSER_DIR`); `IMX_CANDIDATE_ID` selects the candidate (default `default`). Directories are created owner-only (`0700`) and a new state database is created `0600`; files that already exist keep the permissions you gave them. For development, use `IMX_HOME=$PWD/.imx`, which is git-ignored.

## Development

The repository is a uv workspace:

| Package | Role |
| --- | --- |
| `packages/core` | contracts and the SQLite store |
| `packages/candidate` | profile loading and saved answers |
| `packages/generation` | factual form answers |
| `packages/browser` | the Playwright runtime and the local mock ATS (`scripts/mock_ats.py`) |
| `packages/jobs` | OpenCLI discovery, source evidence and listing deduplication |
| `packages/selection` | Jev decisions, preference gates and candidate-scoped caching |
| `packages/pipeline` | tracker, source-preserving imports and revision checks |
| `apps/cli` | the CLI and the reusable runner (`interviewmaxxing_cli.runner`, also used by the local service) |
| `apps/service` | local HTTP bridge, durable tasks and presentation contracts |
| `apps/web` | Next.js jobs browser, pipeline and application desk |

Contracts, import paths and service interfaces are documented in [CONTRACTS.md](CONTRACTS.md); worktree ownership is in [WORKTREES.md](WORKTREES.md).

Run the full verification (locked install, lint, strict type check, tests, CLI smoke, and the end-to-end suite that drives the installed CLI and real Chromium against a separately started localhost mock ATS):

```bash
scripts/verify.sh              # IMX_SKIP_E2E=1 skips the browser suite
uv run pytest e2e              # the end-to-end suite alone
```

Tests only use fictional data and a temporary `IMX_HOME`. On an end-to-end failure, CLI logs, screenshots, HTML and page text are copied to the ignored `e2e/.artifacts/<test>/`.

Verify the dashboard separately from `apps/web`:

```bash
npm run typecheck
npm test
npm run test:e2e
```

The browser suite builds and serves the production frontend on an available local port. The separate frontend-to-Python acceptance harness is documented in [apps/web/README.md](apps/web/README.md). Offline performance prototype tests run with `python3 -m unittest discover -s tests -t .` from `benchmarks/performance`.
