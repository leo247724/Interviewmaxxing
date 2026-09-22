# Supplied-URL MVP build assignments

Coordinator: `j-workspace`; workers: Claude Opus 5.5 through Superset on the local host.
Scope is `ARCHITECTURE.md` sections 2 and 17, not the deferred roadmap.

## Rules for every implementation worker

- Read `ARCHITECTURE.md`, `WORKTREES.md`, and `CONTRACTS.md` when it exists. Verify your worktree and branch before editing.
- Implement only the assigned package and its tests. Do not change another worker's contracts, files, branch, or running processes. Request a specific contract change from the coordinator when needed.
- Use Python 3.12+ through `uv`; the system Python is 3.11. Each worktree uses its own ignored environment, state, artifacts, and ephemeral localhost test ports. Do not add hosted infrastructure.
- Prefer the configured graph tools for code discovery when an index is available. Non-code/configuration reads and missing graph coverage may use `rg`.
- For interactive browser exploration, use the installed `playwright-cli` skill/command first, then configured Playwright MCP if it fails, then `agent-browser`. Verify fresh page state after navigation or a DOM change. Product runtime code and automated tests use the project's Playwright dependency.
- Read applicable project instructions and relevant official dependency documentation. Keep documentation and the actual public API consistent.
- Test against fictional local candidates and localhost forms. Never send a real application, contact an employer, or use the user's credentials while developing. The coordinator handles the expressly requested live acceptance run.
- The product itself must treat a user's `apply` request as submission authorization. Prompt only for missing required information, uncovered personal attestations, or user interaction such as sign-in/CAPTCHA.
- Never infer consent, protected attributes, eligibility, salary, or a factual qualification from unrelated data. Unknown required answers must be exposed as missing input.
- Submission means observed site acceptance, not a click, navigation, or timeout. Persist `SUBMITTING` before the potentially irreversible action. Ambiguous outcome or a crash during that action must block resubmission pending reconciliation.
- Do not push, open PRs, install unrelated global tools, create extra agents, change global settings, or modify the main project checkout. Commit only your allowed changes on your assigned branch after meaningful checks pass. Report the exact commit.
- Finish with the architecture's completion receipt and this envelope:

```text
SUPERSET_WORKER_DONE
task: <task-id>
summary: <concrete behavior implemented>
files: <changed paths>
checks: <commands and results>
handoff: <commit and public interfaces; unresolved limitations>
```

For a true blocker, return `SUPERSET_WORKER_BLOCKED`, `task`, `reason`, and `needs`. Do not claim completion from mocks of the component you were assigned to implement.

## C1 — Core contracts and local foundation

Workspace: `build/core-contracts` (`6a218556-206b-446f-ab32-7659bd383217`).
Dependencies: scope baseline `9111218`; no code exists yet.
Allowed writes: `packages/core/**`, `apps/cli/**`, `tests/core/**`, `tests/fixtures/core/**`, `tests/conftest.py`, `pyproject.toml`, `uv.lock`, `.python-version`, `.gitignore`, `CONTRACTS.md`, `README.md`, `scripts/verify.sh`, and the minimal package marker files required for the chosen package layout. Do not edit architecture, worktree ownership, or coordinator status.

Deliver a small installable Python project with Pydantic contracts and a SQLite store. The package layout must preserve each worker's directory ownership. Choose straightforward import paths, publish them in `CONTRACTS.md`, and provide fixture examples. Do not build candidate, generation, or browser implementations in this task.

Canonical contracts must cover:

- verified candidate identity, factual evidence, a supplied resume artifact, and explicit saved answers;
- application request, minimal job identity, normalized fields/forms, packet answers with provenance, missing inputs, execution results, receipt/evidence references, and events;
- field controls including text, textarea, single select, multiselect, radio, checkbox and file upload; distinguish machine option values from user-visible labels;
- state transitions for normal execution, missing-input resume, clear failure, duplicate detection and uncertain submission;
- typed public service interfaces for candidate loading, packet resolution, browser inspection/fill/navigation/submission/confirmation and CLI integration. Keep browser-facing interfaces asynchronous. Downstream packages import these contracts rather than redeclaring them.

The store must atomically enforce candidate/job uniqueness and state/event persistence, preserve meaningful job URL query parameters, serialize submission attempts, and prevent retry of `SUBMITTED`, `SUBMITTING`, or `SUBMISSION_UNKNOWN`. URL aliases that resolve to the same canonical job need a safe identity-binding operation. A interrupted submission must remain uncertain until acceptance or a definite non-submission can be established. Do not claim an HTTP redirect proves job identity.

Create the `interviewmaxxing` CLI entrypoint with usable help, state/receipt inspection, and a clearly incomplete `apply` skeleton that cannot report success. The final integrated apply/resume flow is task I1. Document local profile/state/artifact conventions without placing personal data in source control.

Acceptance: clean install via `uv sync`, CLI help works, meaningful core tests pass for serialization, request deduplication, alias binding, invalid transitions, event durability, concurrent claims and crash/unknown-submit handling. Define one executable verification command. Publish exact integration signatures and dependency requests for the other workers in the handoff.

## C2 — Verified candidate data

Workspace: `build/candidate-brain` (`4d16e305-b074-4bdd-9ff9-f46a553ccfbf`).
Dependencies: approved C1 contracts, merged into this worktree by the coordinator.
Allowed writes: `packages/candidate/**`, `tests/candidate/**`, `tests/fixtures/candidate/**`, `examples/candidate.example.json`, `examples/answers.example.json`.

Implement the documented candidate service. Load explicitly supplied structured candidate facts, contact information, a local resume and saved answers. Resolve paths predictably, validate file existence and profile shape, attach source/provenance and return actionable errors. Preserve false/zero/empty-versus-missing distinctions. Do not extract speculative qualifications from resume text or invent optional answers. Allow explicitly saved attestations without introducing an extra approval for every application. Scope answers so a job-specific response cannot silently become a global answer.

Use fictional examples. Keep private profile files, resume copies and persisted answers under ignored user storage. Required dependency or contract changes go to the coordinator.

Acceptance: contract-compatible return types; valid profile and resume load; malformed/missing data produce clear errors; explicit false/zero answers survive; unverified facts and conflicting saved answers are not promoted to truth; tests cover relative paths and job-specific answer scope.

## C3 — Factual form answers

Workspace: `build/application-packets` (`7333147f-5e19-441c-b25c-e08dc161282e`).
Dependencies: approved C1 contracts and C2 data conventions. May implement against canonical fixtures once those conventions are settled.
Allowed writes: `packages/generation/**`, `tests/generation/**`, `tests/fixtures/generation/**`.

Implement the documented packet resolver. Map normalized semantic fields to supplied verified facts, the existing resume and explicit saved answers. Match select/radio/multiselect values against actual options without fabricating options. Return provenance for every resolved value and a precise missing-input request for unresolved required fields. Optional unknown fields can remain blank.

For required free text, prefer an explicitly supplied answer. Only assemble grounded factual text when the available facts actually answer the question; unknown motivations, qualifications or attestations must remain unresolved. Do not add an LLM service or Jev dependency just to solve deterministic fields. Prevent similar-but-different questions from reusing an unrelated saved answer.

Acceptance: typed fixture-to-packet tests; truthful provenance; missing required answers; optional omissions; boolean/zero handling; exact option translation; no inferred consent/protected data/eligibility; stable answer keys across a resumed form.

## C4 — Browser inspection and execution

Workspace: `build/browser-ats` (`acff6e42-6b2f-407e-b5c8-0978a8017ca4`).
Dependencies: approved C1 contracts. Actual user target URL is pending; build native HTML and accessible generic form support first, then add the actual ATS adapter when supplied.
Allowed writes: `packages/browser/**`, `packages/ats/**`, `tests/browser/**`, `tests/ats/**`, `tests/fixtures/browser/**`, `scripts/mock_ats.py`.

Implement the documented browser service using real Playwright. Provide a persistent local browser context and visible mode for required user interaction. Inspect current DOM before each action; respect labels, required flags, enabled/visible controls, option labels/values, field groups, uploads, and multistep navigation. Form fields must have stable semantic identities for resume and saved answers. Browser control values are derived from a canonical packet, not from reasoning over untrusted page instructions.

Use the generic adapter for accessible/native controls. Detect unsupported custom controls, sign-in, CAPTCHA and required personal attestations as actionable needs-input states. A saved authorized attestation can be filled. Distinguish forward navigation from final submission; ambiguous controls must not be clicked blindly.

Submission detection requires affirmative evidence tied to this application. Merely disappearing forms, a URL change, generic page text or successful `click()` are insufficient. Capture evidence references and validation errors. After any possibly accepted final action, uncertainty must return `SUBMISSION_UNKNOWN` without automatic retry. Support re-inspecting the same application to reconcile an uncertain outcome.

Provide a deterministic localhost mock ATS server runnable independently, with single-step and multistep forms, native controls, upload handling, missing-input/resume, clear validation rejection and accepted-but-confirmation-unavailable cases. Keep a server-side submission count/receipt endpoint for independent end-to-end assertions. Never make automated tests point to a real employer.

Acceptance: tests launch real Chromium against localhost, inspect/fill/upload/navigate, observe server-side acceptance and confirmation, handle validation failure, surface sign-in/CAPTCHA and unsupported controls, and preserve uncertain outcomes. Report exact commands, dependency requests and evidence paths. Coordinator will use the mock server for full CLI acceptance.

## I1 — Integrated CLI and end-to-end acceptance

Workspace: `build/core-contracts`; assigned after C1-C4 integration.
Allowed writes: C1 allowlist plus `e2e/**`. Other package fixes remain with their owner.

Wire `apply <url>` to load candidate data, claim the application, inspect/resolve/fill each step, ask only required missing questions, submit once, verify acceptance, and persist a receipt and events. Provide a clear noninteractive missing-input result and a documented resume/answer workflow across process restarts. Keep credentials and artifacts in local ignored storage. Present concrete states and next actions; never label an unconfirmed attempt successful.

End-to-end checks must execute the installed CLI and real browser against the separately running localhost mock ATS. Assert both CLI-visible result and server-side submission count, persisted state/events and uploaded content. Cover the happy path, multistep inputs, missing-answer resume after process restart, repeated apply after success, an uncertain submission followed by a blocked retry, and reconciliation. Capture ignored Playwright traces/screenshots for failures.

Acceptance: the repository's documented setup and verification commands work in a fresh environment; unit/integration and full CLI browser tests pass; README explains profile setup, apply, missing-input resume, receipts, uncertain outcomes and the actual supported form/ATS limits. A separately authorized real application with the user's URL and verified information remains required for live acceptance under architecture section 17.
