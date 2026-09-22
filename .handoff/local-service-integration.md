# Remaining local integration packages

All workers follow the scope, fictional-data rules, commit/check receipt and owner boundaries in `mvp-build-tasks.md`. No real applications or employer messages during development. Job discovery and Jev remain deferred. These tasks implement the already requested local frontend/backend.

## C2P — Candidate setup and supplied resumes

Owner: candidate-brain, C2 allowlist only. Start after C2R is committed.

Add narrow package APIs to let the frontend store an uploaded resume before a complete profile exists, list/select supplied resumes, and create/update the user's contact profile. Candidate identity comes from the configured stable candidate ID, never a content hash or a fresh ID per request. Updating contact details or selecting another resume must not erase facts, experience, education, saved-answer scope/conflicts, or application history.

Use canonical CandidateIdentity and ResumeArtifact. A complete CandidateProfile continues to require an actual supplied resume. Keep incomplete UI setup separate from canonical candidate truth; do not invent identity or an empty resume artifact. Explicit user-entered contact information may carry its actual confirmation time. Resume files/catalog belong under the candidate's private directory, immutable and owner-only; use safe generated IDs, bounded upload size, safe filename handling, atomic persistence and the existing lock. No extraction or upgrading of factual qualifications.

Publish exact API signatures for storing bytes, listing/getting uploads, and upserting contact identity plus a selected resume. A practical seam is LocalCandidateStore.store_resume(candidate_id, *, filename, content: bytes, media_type), list_resumes(candidate_id), get_resume(candidate_id, resume_id), upsert_profile(candidate_id, *, identity: CandidateIdentity, resume_id: str). You may choose a smaller equivalent if documented immediately. Include imported/manual profiles and their existing resume in the UI-facing result without silently copying arbitrary files from a request path. Profile writes must preserve the raw profile's embedded answers, not duplicate reconciled answers from answers.json.

Verify initial upload/setup, profile updates preserving facts/answers, stable ID and resume selection across reload, concurrent operations, malformed/traversal filenames and IDs, rejected oversize/empty input, digest correctness, and private file permissions. No root dependency edits. Commit and return SUPERSET_WORKER_DONE task C2P.

## S1 — Local HTTP service for the existing frontend

Owner: queue-runtime, newly activated for this bounded local task. Allowed writes: `apps/service/**`, `tests/service/**`, `tests/fixtures/service/**` only. Package name `interviewmaxxing-service`; package source `interviewmaxxing_service`. Root dependencies and shared contracts stay core/coordinator-owned. Read the frontend's current service types from the dashboard worktree without editing them. Dependencies: integrated C2/C3/C4 and I1, plus C2P. Before those land, the worker may implement HTTP/view mapping against the canonical store and injected executor protocol, but must finish real integration before claiming S1 complete.

Use a small loopback-only Python service (standard-library HTTP is sufficient) exposing the frontend's routes: GET /candidate; POST /resumes; POST /applications; GET /applications/{id}; POST /applications/{id}/answers, /resume, /reconcile. Return exactly the presentation models in `apps/web/lib/service/types.ts` and structured errors `{error:{code,message,fieldErrors?}}`. Use 404 only for genuinely absent records. Expose a readiness/health endpoint with no private data.

The service maps canonical state and evidence to presentation views. It uses the configured stable candidate ID, C2P for profile/resume writes, and the I1 runner for all execution and browser reconciliation. Record an application synchronously through the canonical store before dispatch, then return its ID promptly for polling while the runner works. Do not lose an application ID on a client disconnect. Serialize execution against the persistent browser profile, reject or report concurrent conflicting work, and retain durable unknown state after an interrupted submit. Do not add Redis, a hosted platform, or a second submission state machine. A background thread/event loop may execute the canonical async runner; each store connection respects SQLite's thread ownership.

Save answers using the latest canonical missing inputs and UserInput.answering, preserving exact wording/options/fingerprints. The default scope is this application unless the UI explicitly chooses broader reuse. Reject stale/unrecognized questions and invalid choices before any resume. Do not treat a non-final page as a submission.

Reconciliation rechecks actual site evidence through I1. User reports of confirmation or non-receipt remain clearly attributed user evidence; they must never be silently promoted to a site-confirmed receipt or definite permission for a second submission. Keep unknown state when proof is insufficient. Surface useful limitations in the UI response.

Serve evidence only by an application-scoped opaque identifier resolved from the canonical evidence record under the artifacts root. Protect traversal, never expose arbitrary file paths, and return correct content type and download disposition for unsafe document types. No private paths or credentials in logs.

Restrict Host and bind address to loopback; protect mutations against cross-site use with an exact configured frontend Origin and appropriate content types. The Next gateway will validate its own incoming Origin and send the configured approved Origin upstream. No permissive CORS. Use request/upload bounds and actionable errors. Raw resume upload `application/octet-stream` with a safely encoded filename header is acceptable at the Python boundary; the Next gateway can translate its browser multipart request. Document the precise transport for F2.

Verify HTTP contracts, status/error mapping, profile/upload persistence, exact question options, candidate isolation, local-origin protections, background dispatch and recovery. Final acceptance must exercise the real I1 runner/browser against localhost fixture and assert server acceptance count, receipt, repeat request, missing-input reload and uncertain reconciliation. Mocked executor tests alone do not finish S1. Commit and return exact setup command/public signatures and SUPERSET_WORKER_DONE.

## I1 shared runner seam for S1

Core owns the concrete reusable async runner and CLI. Publish a constructor/factory with LocalPaths, headless/visible browser selection and noninteractive UserInteraction; methods apply(url, candidate_id=), resume(application_id), and browser-backed reconcile(application_id). The HTTP service should import this API directly, not scrape CLI prose or duplicate its control flow. The store remains the sole state authority. Core and service owners must send exact proposed signatures before integration; package-only adapters can accommodate differences without expanding core schemas.

## F2 integration expectations

Dashboard retains `apps/web/**` ownership and its existing UI service types. Connect the same-origin route to S1, transform multipart uploads if required, and validate the incoming browser Origin before mutations. Keep body data out of shell commands and URLs. No mock responses on the live route. Preserve active IDs on transient service failures and across reloads. Verify real frontend -> S1 -> I1 -> Chromium -> mock ATS receipt/count, missing-answer reload, duplicate and unknown reconciliation; add the production build and responsive UI checks. Preserve the existing polished visual direction and truthful limitations.
