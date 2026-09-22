# Pipeline backend and complete frontend

User explicitly requested a pipeline-style tracker and supplied a Numbers workbook as the reference. The original file has one Pipeline sheet / Table 1, 23 logical headers (first row, despite native header count zero), and eight records. All private records are outside Git; only schema and fictional examples belong in commits.

## Reference schema

| Source header | Field |
| --- | --- |
| Company | company |
| Role | role |
| Stage | stage |
| Status | status |
| Priority | priority |
| Fit / 10 | fitScore |
| Next interview date | nextInterviewDate |
| Time (CT) | interviewTimeCT |
| Interview format | interviewFormat |
| Work arrangement | workArrangement |
| Location / commute | locationCommute |
| Comp low (USD/year) | compensationLow |
| Comp high (USD/year) | compensationHigh |
| Comp basis | compensationBasis |
| Target assessment | targetAssessment |
| Source / recruiter | sourceRecruiter |
| Follow-up date (suggested) | suggestedFollowUpDate |
| Next action | nextAction |
| Last interview date | lastInterviewDate |
| Decision due | decisionDueText |
| Comp / benefits notes | compensationBenefitsNotes |
| Fit rationale | fitRationale |
| Process / source notes | processSourceNotes |

Stage/status are free text, including detailed interview descriptions; preserve them. Dates normalize to YYYY-MM-DD; Decision due remains text. Interview time is explicitly CT (America/Chicago). No next interviews are currently dated. The five suggested follow-ups are historical, not newly scheduled by this app. Imported fit/10 is the user's score, never Jev confidence. Location/commute and work arrangement remain separate.

## P1 — Durable pipeline and import

Owner: application-packets, newly assigned after reviewed generation C3 completion. Allowed writes: `packages/pipeline/**`, `tests/pipeline/**`, `tests/fixtures/pipeline/**`, `examples/pipeline-import.example.json`. Package interviewmaxxing-pipeline / interviewmaxxing_pipeline. Do not edit packages/generation or another owner while building this independent package. Core D0 provides an additive PipelineEntry contract; detailed tracking/import DTOs may live in this package while the canonical application state remains core-owned.

Build a local durable pipeline repository with list/get/create/update/move operations, stable candidate-scoped identities, notes/next actions/due dates, optional links to a discovered listing and a canonical application, and history of user stage changes. Imported/manual jobs may have no listing/application URL; do not invent a URL or manufacture an ApplicationStore record/receipt. Stage moves never establish site-confirmed submission. Keep imported source stage/status and source provenance intact alongside a separately editable board lane. Preserve all 23 reference fields, not only the card subset.

Expose configurable readable board lanes and a conservative initial mapping (saved, applied, scheduling, interviewing, assessment, follow-up, decision, offer, closed). Never infer a concrete interview date or employer outcome from adjacent notes. Detailed raw stage/status remain visible and editable as user tracking data; provenance retains the original import values. Notes and recruiter names never trigger outreach.

Support validated normalized JSON and CSV import with a preview/receipt, stable idempotent keys and source-row provenance. Reimporting unchanged data cannot duplicate cards or erase subsequent manual edits. Malformed rows must be reported precisely; avoid partial silent imports. Validate compensation bounds and user score range without fabricating missing values. Protect candidate isolation, stable links, concurrent updates, and non-finite/malformed JSON. Private SQLite/file storage under the configured local home; do not reuse or modify core application tables. No third-party database/hosted platform needed.

The coordinator will provide a private normalized export of the real workbook for an explicit local import after tests pass. Do not add real rows to test fixtures. An employer homepage hyperlink in the workbook is not a job application URL. Preserve blanks separately from zero and record the source digest/import timestamp. Provide exact Python API signatures and import command (package-local CLI is fine; core later wires overall CLI). S1 will call this package for pipeline endpoints; frontend uses those endpoints.

Acceptance: fictional tests for all 23 fields, persistence/reload, move/history, editable notes/next actions, candidate isolation, concurrent update conflicts, idempotent import, malformed rows, preservation of manual edits and no generated application receipts. Verify real normalized export can be previewed/read without committing it; actual user's local import is coordinator-owned after review. Commit only allowlisted files and return SUPERSET_WORKER_DONE task P1 with exact APIs.

## F2/F3 — Connected application desk, jobs and pipeline

Owner: dashboard; allowed writes apps/web/** only. F1/F1R/F1R2 are independently reviewed and approved. Maintain its established visual direction and responsive/accessibility quality. Read installed frontend-design skill and local current Next documentation as required by apps/web instructions.

First wire the existing application desk to S1 using the approved service shape. S1 code is in sibling build/queue-runtime/apps/service and the task scope is local-service-integration.md. Implement exact same-origin checks on mutation requests before proxying; set the verified frontend Origin upstream. Transform browser multipart resume uploads to S1's documented bounded transport if needed. Preserve all restore fixes, full question wording and actual option values. Never fabricate live success while services are unavailable.

Add a navigable Pipeline board and Jobs browser beside the application desk. The tracker is a primary view with readable cards (company/role, stage/status, priority, compensation, next action/due), real stage movement and detailed editing of all reference fields. Provide keyboard-accessible move controls even if dragging is supported, plus a useful list/table alternative on small screens. Existing imported rows with no application URL must ask for that URL before applying. Keep manual tracking, Jev decisions and confirmed application receipts visually distinct.

Jobs search exposes editable keywords and preferences with defaults: marketing manager / marketing director, Austin onsite/hybrid plus US-wide remote, USD100000 annual minimum. Do not restrict remote jobs to Texas. Show per-source loading/needs-login/challenge/error state; results have actual source/application links and observed details. Jev decisions expose APPLY/SKIP/REVIEW and evidence/reasons, with unknown facts visibly unresolved. Applying a selected job enters the same application desk/state, with one backend duplicate check. No auto-submit from a card merely being imported or recommended.

Use typed frontend presentation interfaces for pipeline/search/selection, document proposed S1 route contracts early in apps/web/README.md so coordinator can hand them over. Suggested route groups: /pipeline with per-item updates/moves/import, /jobs/search and /jobs list/detail, /selection/preferences and per-job decision. Core D0 plus packages/jobs, selection and pipeline supply the domain data. Until those dependencies land, fixture previews remain explicit and separate from live routes. You may build UI and boundary contracts in parallel, but final task completion requires real services and full E2E.

Use fictional preview records shaped like the Numbers reference. Never commit the user's eight real records or narrative/recruiter notes. Final acceptance: production build, meaningful unit/interaction checks, desktop/mobile visual verification, actual frontend -> S1 -> I1 -> Chromium -> localhost ATS receipt/count, missing-answer reload, duplicate, uncertainty/reconcile, pipeline persist/reload/move/import and jobs/Jev state handling. Return a clean checkpoint if awaiting a dependency, with exact missing seam, then continue integration when supplied. No unused controls or placeholder successful responses in the completed MVP.
