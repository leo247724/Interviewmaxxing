# Interviewmaxxing

An open-source job-search system that optimizes for interviews and offers, not application count. See [ARCHITECTURE.md](ARCHITECTURE.md).

**Current milestone:** you give it the URL of a job application you chose; it fills and submits that application with your verified profile and resume, verifies the site's confirmation, and saves a receipt. Asking to apply authorizes the submission. You are asked only for missing required information or for actions such as sign-in or CAPTCHA.

> **Status:** core contracts, the local state store and the CLI skeleton are in place. `interviewmaxxing apply` currently records the request and checks for duplicates, then exits with status 3 (`INCOMPLETE`). It does not open forms or submit anything yet; that arrives with integration task I1.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 automatically from `.python-version`)

## Install

```bash
uv sync --all-packages
uv run interviewmaxxing --help
```

## Commands

| Command | Purpose |
| --- | --- |
| `interviewmaxxing apply URL [--candidate ID]` | Apply to the job at `URL` (skeleton: records the request only) |
| `interviewmaxxing status [APPLICATION_ID] [--json]` | List applications, or show one with attempts and missing input |
| `interviewmaxxing events APPLICATION_ID [--json]` | Event history |
| `interviewmaxxing receipt APPLICATION_ID [--json]` | Receipt of a confirmed submission |
| `interviewmaxxing reconcile APPLICATION_ID (--accepted \| --not-submitted) --detail TEXT` | Resolve an uncertain submission after checking with the employer |
| `interviewmaxxing paths [--json]` | Show where local data lives |

Exit status: `0` ok, `1` error, `2` usage, `3` not submitted / incomplete, `4` blocked by stored state (already submitted, submission uncertain, no receipt).

An application is reported as submitted only after the site's acceptance is observed. If a submit may have reached the employer but no confirmation was seen (a timeout or crash), the application becomes `SUBMISSION_UNKNOWN` and is never retried automatically; use `reconcile` once you know the outcome.

## Local data

Everything personal stays on your machine under `IMX_HOME` (default `~/.interviewmaxxing`) and out of source control:

```
$IMX_HOME/profile/             your profile, resume and saved answers
$IMX_HOME/state/imx.sqlite3    application requests, states and events
$IMX_HOME/artifacts/<app-id>/  confirmation screenshots and other evidence
$IMX_HOME/browser/             persistent browser profile (sign-ins)
```

Each location can be overridden (`IMX_PROFILE_DIR`, `IMX_STATE_DB`, `IMX_ARTIFACTS_DIR`, `IMX_BROWSER_DIR`); `IMX_CANDIDATE_ID` selects the candidate (default `default`). For development, use `IMX_HOME=$PWD/.imx`, which is git-ignored.

## Development

The repository is a uv workspace: `packages/core` (contracts and store), `apps/cli`, and reserved `packages/candidate`, `packages/generation`, `packages/browser` for the other workers. Contracts, import paths and service interfaces are documented in [CONTRACTS.md](CONTRACTS.md); worktree ownership is in [WORKTREES.md](WORKTREES.md).

Run the full verification (locked install, lint, strict type check, tests, CLI smoke):

```bash
scripts/verify.sh
```

Tests only use fictional data and a temporary `IMX_HOME`.
