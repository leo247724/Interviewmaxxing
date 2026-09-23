# Final backend integration receipt — 2026-09-22

Service integration owner: `build/queue-runtime`. Code checkpoint: `bd45264`.
This receipt covers the service and committed backend packages; dashboard-driven
acceptance and the coordinator's final workspace lock validation are separate.

## Integrated dependency commits

| Package | Final integrated commit |
| --- | --- |
| Core and installed CLI | `f38d0d116f9e2df7f698f1390f872c5684d7f35b` (includes `90ec571`, I1R `1408302` and rejection fix `72a10ea`) |
| Browser | `c3a3c41d7e3ebf6fb2f3978c54a0b9ed62838958` (includes O1R `0e4273a` and C4R4) |
| Jobs | `ab8f22dde35c2b170dfd32588571259bb29043e0` (includes J1R `0a02462`) |
| Selection | `d9330698d07ba38911c86eee1d6661939724b042` (includes J2R `acdd2e3`) |
| Pipeline | `e5c14c7090b70ca9fb80ecb5870e1d03821bb6db` (P1R2) |
| Candidate | `caae823e93b6eb0749dc9c093285aaf08980080e` inherited root checkpoint (C2P/C2P2) |

All merges preserve the existing S3R service branch; no dependency worktree's
uncommitted files were read or merged. Owned edits are only `apps/service/**` and
`tests/service/**`, plus explicit dependency merge commits.

## Service changes

- Candidate-scoped native selection batches and candidate-aware currentness when no
  profile exists. Existing `DONE`, `FAILED`, `INTERRUPTED`, `postingUrl` and linked
  receipt-authority DTOs are preserved.
- Exclusive OS ownership of the shared service state before startup recovery.
  Duplicate same/different-candidate service launches cannot interrupt live tasks
  or duplicate provider work. Search/decision workers stop before releasing the
  lease; a crashed process releases its OS lock automatically.
- Optional `pipelineEntryId`/`listingId` application handoffs validate candidate,
  saved IDs, known URLs and known canonical ATS identity conflicts before dispatch.
  The canonical application is recorded and its resume pinned before the pipeline
  link is written. Failed link writes never dispatch; retry reuses the recorded
  application. A different existing application link is never overwritten.
- A known proven listing ATS key is durably pinned before link/dispatch. The runner
  checks fresh observed identity before fill/advance/submit. Missing or conflicting
  observations fail retryably without submission; expectations survive restart and
  cannot be replaced on repeat. Existing protected applications without a pin need
  an already-observed matching identity; the service never adds a late pin.
- J1's persisted aliases preserve pipeline cards, decisions and durable task polling
  across canonical listing merges. A queued decision remains coalesced and visible
  after the merge. Existing candidate-owned listing provenance is not rewritten.
- HTTP acceptance now checks receipt/uncertainty on linked cards and repeated site
  rejection corrections across a service restart.

## Verification actually run

Dedicated ignored `.venv-task`, Python 3.12 and existing locked Playwright 1.62.0.
No packages were upgraded and no root lock was changed.

- Combined `python -m pytest tests e2e -q` at `bd45264`: **1,564 passed, 7 skipped**,
  132.90 seconds. This includes the final dependency commits above, full backend
  tests, the actual HTTP service and installed CLI driving Chromium against a
  separately running localhost mock ATS.
- Focused expected-identity and application-handoff tests: **15 passed**, including
  wrong/missing first observation, persisted restart expectation, pre-submit drift,
  matching identity, known existing-receipt conflict, and cross-store retry.
- Independent runtime reviewer approved `bd45264` after **41 passing** archived
  probes/regressions: eight identity/wrong-link cases plus 33 ownership, S3R, alias,
  handoff and profile-failure checks. That reviewer used scripted browsers only;
  its result does not substitute for the separate real-Chromium review.
- `ruff check apps/service tests/service`: passed.
- `mypy --strict apps/service/src`: passed, 19 source files.
- `git diff --check`: passed.

The seven opt-in skips are five live OpenCLI smoke tests, one live job-source smoke
test, and one private pipeline-reference import. Their owner-reported live browser
receipts are not represented as tests run by this service worker.

All this worker's application tests used fictional candidates and temporary homes,
local mock ATS processes, and injected provider transports. No real profile, real
application, live job source or paid Jev call was used. The user's candidate JSON
remains pending; nothing in this receipt asserts it was configured or validated.

## Limits

`TEST_ONLY` remains the default. Its service guard admits only loopback request URLs
for start/resume/recheck; it does not inspect browser redirect or apply-link chains.
This is URL admission, not complete network-egress isolation.

Expected-identity enforcement applies when the selected listing carries a proven
employer/ATS job key. A direct URL or listing without such a key retains the existing
URL-driven runner behavior; this guard does not invent identity evidence.

The application and pipeline stores are separate. A failed link write can leave a
recorded, pinned, unstarted request; the same explicit handoff safely completes the
link on retry. Existing unrelated cards/rows are never deleted or merged by these
service fixes.
