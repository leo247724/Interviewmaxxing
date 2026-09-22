# Interviewmaxxing Architecture

> **Interviewmaxxing** is an open-source, high-throughput job-search operating system that optimizes for **interviews and offers**, not raw application count.

## 1. Product thesis

Most job-search tools optimize for activity:

```
jobs found -> applications submitted
```

Interviewmaxxing optimizes for outcomes:

```
jobs discovered
    -> jobs qualified
    -> applications submitted
    -> recruiter screens
    -> hiring-manager interviews
    -> final rounds
    -> offers
```

The system should learn which jobs, resume variants, application strategies, and outreach patterns maximize:

```
P(interview | candidate, job, application strategy)
P(offer | interview process, candidate, job)
Expected offer value
```

The product is not one giant autonomous agent. It is a pipeline of typed, replaceable services with explicit contracts.

**Current MVP:** reliable supplied-URL applications, a local frontend and pipeline tracker, OpenCLI job browsing, and Jev application selection. The user expanded the running build on 2026-09-22: find marketing manager/director roles in Austin onsite/hybrid or US-wide remote, with a USD 100000 annual compensation minimum. The supplied-URL flow remains available independently.

**Jev, from [TypeSafe AI](https://typesafe.ai/), decides which discovered jobs fit the candidate.** The local backend calls it through the user-funded OpenRouter Decisions API. Selection records evidence and APPLY/SKIP/REVIEW separately from a real application receipt. The requested-application executor remains the single submission path.

---

## 2. MVP

The first end-to-end milestone is:

```bash
interviewmaxxing apply <application-url>
```

The local Next.js frontend exposes the same flow: enter a URL, supply candidate information and a resume, resolve genuinely missing inputs, and receive the saved submission receipt. The frontend uses the Python executor's state and duplicate protection rather than creating a second execution path.

That command should:

1. Record the user's application request and load their verified profile, resume, and saved answers.
2. Open the supplied URL, identify the job and ATS, and check the local record for an existing submission.
3. Inspect the live form and normalize its fields into an `ApplicationForm`.
4. Build an `ApplicationPacket` using the supplied resume and factual answers.
5. Fill fields, upload documents, and navigate application steps.
6. Ask for missing or ambiguous required information, then resume from the saved state.
7. Validate the completed form and submit the requested application.
8. Verify the site's submission confirmation and persist the result, evidence, and event history.

The request to apply authorizes submission using the user's supplied information and instructions. Additional confirmation is needed only for a material unanswered question, an uncovered personal attestation, or an interaction requiring the user, such as sign-in or CAPTCHA. Do not add a routine second approval step to every application.

The first deliverable includes actual submission and confirmation. Filling a form alone is incomplete. Do this for **one URL correctly** before adding job discovery, job selection, or large-scale concurrency.

---

## 3. Core architecture

### Current MVP

```
User-provided application URL + verified candidate profile/resume
    -> record request and check for prior submission
    -> inspect the live application form
    -> resolve answers and prepare the packet
    -> fill, upload and navigate
    -> ask for missing input only when needed
    -> submit
    -> verify confirmation and save the receipt
```

The browser extracts the minimum job identity and page context needed to apply and avoid duplicates. This does not require the job-discovery service, a `JobMatch`, a Jev call, or a P0/P1/P2 ranking.

### Discovery and selection; outcome learning remains later

```
                         +---------------------------+
                         |       CONTROL PLANE       |
                         | goals / rules / budgets   |
                         +-------------+-------------+
                                       |
                                       v
+-------------+   +-----------+   +---------+   +---------+
| Job Sources |-->| Normalize |-->| Dedupe  |-->|   Jev   |
+-------------+   +-----------+   +---------+   +----+----+
                                                        |
                                     +------------------+------------------+
                                     |                                     |
                                     v                                     v
                                DROP / ARCHIVE                         ROUTE P0/P1/P2
                                                                           |
                                                                           v
                                                              +------------------------+
                                                              | Application Packet     |
                                                              | resume + answers       |
                                                              +-----------+------------+
                                                                          |
                                                                          v
                                                              +------------------------+
                                                              | Browser Runtime        |
                                                              | inspect / plan / fill  |
                                                              +-----------+------------+
                                                                          |
                                                   +----------------------+----------------------+
                                                   |                                             |
                                                   v                                             v
                                           Human Review                                  Submit / Persist
                                                   |                                             |
                                                   +----------------------+----------------------+
                                                                          |
                                                                          v
                                                          +-------------------------------+
                                                          | Outcomes / Email / Interviews |
                                                          +---------------+---------------+
                                                                          |
                                                                          v
                                                          +-------------------------------+
                                                          | Feedback / Ranking Model      |
                                                          +-------------------------------+
```

In the discovery flow, Jev's job-selection decision happens before packet generation. `APPLY` enters the P0/P1/P2 application routes, `SKIP` archives the job, and `REVIEW` waits for job-selection review. The user-provided URL flow enters application execution directly.

---

## 4. Technical stack

### Backend / orchestration
- **Python 3.12+**
- **Pydantic**
- A local CLI runs one application at a time.
- **SQLite** stores application requests, events, submission state, and duplicate checks for the MVP; local files hold supporting artifacts.
- **FastAPI**, **PostgreSQL**, **Redis**, and a distributed queue runtime are later additions when a hosted or concurrent workflow needs them.

### Browser runtime
- **Playwright**
- **OpenCLI + Browser Bridge** for user-present workflows in an existing Chrome session, including application follow-up and assessments.
- DOM-first inspection.
- Browser agents execute plans; they should not own career strategy.

### Frontend
- MVP: **Next.js / TypeScript** interface for application URL entry, candidate/profile and resume setup, progress, missing questions, submission receipts, job search/selection, and a pipeline board based on the user's supplied tracker.
- The CLI remains the local execution and diagnostic interface, with a visible browser when user interaction is needed. A narrow local server-side bridge connects the frontend to the same executor and durable state.
- Later: large-scale queues and outcome-learning analytics. The MVP already includes editable job tracking stages, interview/follow-up fields and notes.

### Models
- **GPT Astra**: project orchestrator / integration manager.
- **Fable 5.x**: optional specialist for difficult investigations; not part of the current eight-worker roster.
- **Opus 5.5**: default implementation worker for bounded feature work.
- **TypeSafe AI / Jev through OpenRouter**: product decision maker for which discovered jobs to apply for; evaluates candidate/job fit and returns `APPLY`, `SKIP`, or `REVIEW`.
- Optional local/open-source models for cheap generation and classification.

---

## 5. Canonical domain objects

These contracts belong to the core worktree and must not be independently redefined by other agents.

The MVP implements the subset needed for a supplied URL: candidate data, the application request, minimal job identity, form, packet, application state, and events. Discovery/scoring fields and cross-language schemas are later work.

### ApplicationRequest

```python
class ApplicationRequest:
    id: str
    candidate_id: str
    application_url: str
    requested_at: datetime
    selection_source: Literal["USER_PROVIDED"]
```

Record the user's request directly. A user-provided URL does not produce a synthetic Jev decision.

### CandidateProfile
Structured source of truth about the candidate.

```python
class CandidateProfile:
    id: str
    identity: CandidateIdentity
    experience: list[Experience]
    skills: list[SkillEvidence]
    education: list[Education]
    projects: list[Project]
    preferences: CandidatePreferences
    facts: list[CandidateFact]
```

### CandidateFact

Every generated claim should trace back to a candidate fact.

```python
class CandidateFact:
    id: str
    key: str
    value: object
    source: str
    confidence: float
    evidence: list[str]
```

Example:

```yaml
id: djc.monthly_spend
key: monthly_paid_media_spend
value: 400000
source: resume
confidence: 1.0
```

### JobPosting

```python
class JobPosting:
    id: str
    source: str
    source_url: str
    company: str
    title: str
    location: str | None
    remote_type: str | None
    salary_low: int | None
    salary_high: int | None
    description_raw: str
    description_clean: str
    ats_type: str | None
    apply_url: str
    posted_at: datetime | None
    discovered_at: datetime
    fingerprint: str
```

### JobMatch — automated selection

```python
class JobMatch:
    job_id: str
    candidate_id: str
    role_family: str
    seniority: str
    fit_score: float
    interview_probability: float | None
    confidence: float
    decision: Literal["APPLY", "SKIP", "REVIEW"]
    decision_probabilities: dict[str, float]
    decision_model: str
    decision_rubric_version: str
    priority: Literal["P0", "P1", "P2", "DROP"] | None
    resume_variant: str | None
    reasons: list[str]
```

### ApplicationForm

```python
class ApplicationField:
    id: str
    label: str
    semantic_type: str
    control_type: str
    selector: str
    required: bool
    options: list[str] | None

class ApplicationForm:
    url: str
    ats_type: str
    step: int
    fields: list[ApplicationField]
```

### ApplicationPacket

```python
class ApplicationPacket:
    job_id: str
    candidate_id: str
    resume_variant: str
    answers: dict[str, object]
    cover_letter: str | None
    confidence: dict[str, float]
    unresolved_fields: list[str]
```

### Application

```python
class Application:
    id: str
    request_id: str
    job_id: str
    candidate_id: str
    state: str
    packet_id: str | None
    submitted_at: datetime | None
    failure_reason: str | None
```

---

## 6. Application state machine

Current URL-submission flow:

```
REQUESTED
  -> INSPECTING
  -> PACKET_READY
  -> FILLING
  -> SUBMITTING
  -> SUBMITTED
```

Inspection and filling may repeat for multiple pages. Missing required information moves the application to `NEEDS_INPUT`; after the user answers, re-inspect the current page and resume. `NEEDS_INPUT` is a conditional branch, not a mandatory approval step.

Additional states:

```
NEEDS_INPUT
SUBMISSION_UNKNOWN
FAILED_RETRYABLE
FAILED_PERMANENT
DUPLICATE
WITHDRAWN
```

Mark `SUBMITTED` only after observing acceptance evidence from the site. If the submit action may have succeeded but confirmation is unavailable, use `SUBMISSION_UNKNOWN` and reconcile before retrying. A click or a timeout must not cause an unverified success or a duplicate application.

Discovery/scoring/queue states and recruiter-screen/interview/offer outcomes are later extensions.

Every transition must emit an event.

Example:

```json
{
  "event": "application.submitted",
  "application_id": "app_123",
  "timestamp": "2026-09-22T20:00:00Z",
  "metadata": {
    "ats": "greenhouse",
    "resume_variant": "paid_media_v4"
  }
}
```

---

## 7. Field ontology

Normalize every application field into a known semantic type.

```
FIRST_NAME
LAST_NAME
EMAIL
PHONE
ADDRESS
CITY
STATE
ZIP

RESUME
COVER_LETTER

LINKEDIN
WEBSITE
GITHUB

WORK_AUTHORIZATION
SPONSORSHIP
SALARY_EXPECTATION

EDUCATION_LEVEL
UNIVERSITY
DEGREE

YEARS_EXPERIENCE
CUSTOM_LONG_TEXT
CUSTOM_BOOLEAN
CUSTOM_SELECT
CUSTOM_MULTISELECT
```

The application packet should answer semantic fields, not raw DOM selectors.

---

## 8. Jev — deciding which jobs to apply for

This section describes the activated job-discovery flow. Jev uses `POST https://openrouter.ai/api/alpha/decisions` with requested model `typesafe/jev-1.13`; persist the actual returned model. Read OPENROUTER_API_KEY only on the backend from the environment or an explicitly configured ignored env.local file. The supplied-URL apply flow does not require a Jev call.

**Jev owns the job-selection judgment.** The inputs are the normalized job posting, verified candidate experience and skills, compensation and location preferences, career goals, and the job-selection rubric.

```
JobPosting + CandidateProfile + selection rubric
    -> Jev fit assessments
    -> Jev application decision
    -> APPLY / SKIP / REVIEW
```

Use focused Jev questions to assess the factors below. Supply those assessments and their uncertainty to the final Jev selection question. This keeps the selection grounded in explicit evidence while letting Jev decide whether the job is worth applying for.

Examples:

```
role_family
seniority
experience_match
compensation_likelihood
leadership_match
technical_ai_match
location_match
remote_compatibility
application_complexity
human_outreach_value
```

Use TypeSafe's `Choice` primitive for the final selection. It returns the chosen option, probabilities across the supplied options, and confidence. These are provider outputs; the meanings below are Interviewmaxxing's job-selection contract. [TypeSafe Choice documentation](https://docs.typesafe.ai/primitives/choice)

| Jev decision | Meaning | Pipeline behavior |
| --- | --- | --- |
| `APPLY` | The job is worth pursuing for this candidate. | Assign P0/P1/P2 effort and prepare the application. |
| `SKIP` | The job does not meet the candidate's selection criteria. | Record the decision and archive as `DROP`. |
| `REVIEW` | The evidence is insufficient or the tradeoff needs candidate input. | Hold for job-selection review; leave priority unset. |

Code enforces the candidate's explicit hard constraints, confidence thresholds, and application budgets. Missing required evidence or insufficient confidence holds the job for review. A provider failure holds selection for recovery and cannot authorize an application. Confidence thresholds must be evaluated against our own labeled job fixtures. [TypeSafe confidence documentation](https://docs.typesafe.ai/confidence)

Persist the candidate/job evidence snapshot, Jev's original answer and probabilities, returned model version, rubric version, and effective routing decision. Record any policy hold or human override separately so the model's judgment remains auditable. `JobMatch.decision` is the effective decision after those checks; its probabilities and confidence describe Jev's original selection. Build the existing `reasons` from the stored fit assessments and source evidence.

Jev's decision confidence describes uncertainty about job selection. `interview_probability` remains unset until an outcome model has been evaluated against observed interview results.

Effort routing for selected jobs:

### P0
Exceptional fit.
- custom resume
- custom answers
- recruiter/hiring-manager enrichment
- human review
- apply

### P1
Strong fit.
- best resume variant
- tailored answers
- apply

### P2
Acceptable.
- standard resume variant
- standardized truthful answers
- apply

### DROP
Hard mismatch or low expected value.

Fit scores support Jev's selection and ordering of selected jobs. Version the selection rubric and evaluate changes using candidate-labeled apply/skip/review examples.

Long-term objective:

```
ExpectedOfferValue =
    P(interview | candidate, job)
    * P(offer | interview, candidate, job)
    * ExpectedTotalComp
```

---

## 9. Browser runtime

The browser runtime is both a **live form inspector** and an **executor**.

It should not be the system's main reasoning engine.

```
Playwright Page
    -> DOM Inspector
    -> Normalized ApplicationForm
    -> Field Resolver
    -> ApplicationPacket
    -> Form Plan
    -> Executor
    -> Validation Inspector
    -> next page / needs input / submit
```

Core components:

```python
class BrowserSession: ...
class DOMInspector: ...
class FieldResolver: ...
class FormPlanner: ...
class FormExecutor: ...
class ValidationInspector: ...
class SubmissionDetector: ...
```

Rules:
- Inspect the current page before acting.
- Build a complete form plan before execution where practical.
- Deterministic fields should not require an LLM.
- Unknown or unsupported questions route back to the reasoning layer.
- Ambiguous candidate facts route to human review.
- Never allow generated application claims that are not grounded in the candidate fact store.

### OpenCLI and user-present workflows

Use a named OpenCLI session to expose the current page as structured CLI observations and actions. Keep that session and tab stable while the user participates. Read the actual question, options, and surrounding instructions after each page change; use a screenshot only when the question depends on a diagram or other visual content.

The user may retain all browser actions while the assistant reads questions and suggests answers. Record who owns navigation and submission for the session. A request for advice does not itself authorize clicking an answer or starting a timed assessment. Identify the timer boundary before starting; when the user starts a timed section, prioritize the live interaction and avoid other browser work that could change focus. Respect site access controls and preserve visible lockout or failure states.

Suggestions about the candidate must be grounded in their supplied facts and preferences. A broad trait such as hardworking does not answer unrelated questions about sociability, risk tolerance, or specific past behavior. Capture missing information instead of treating an unknown answer as a neutral preference. Keep follow-up/assessment completion separate from a job-application submission receipt.

Reusable site commands should use observed UI semantics, explicit arguments, structured errors, and local verification. Do not commit invitation tokens, real assessment questions, answers, or candidate data. The OpenCLI path shares candidate provenance and durable state with the existing executor; it must not introduce a second submission state machine. Discovery and Jev selection are active through their own bounded worker packages.

---

## 10. ATS adapter interface

```python
class ATSAdapter(Protocol):
    async def detect(self, page) -> bool: ...
    async def inspect(self, page) -> ApplicationForm: ...
    async def fill(self, page, packet: ApplicationPacket) -> FillResult: ...
    async def next(self, page) -> PageResult: ...
    async def detect_submission(self, page) -> bool: ...
```

For the MVP, support the ATS encountered at the first user-provided application URL, using the generic browser runtime and a focused adapter where needed. The broader adapter backlog is:

```
GreenhouseAdapter
LeverAdapter
AshbyAdapter
WorkdayAdapter
RipplingAdapter
SmartRecruitersAdapter
GenericAdapter
```

The generic browser runtime remains the fallback.

---

## 11. Queues (later) and submission idempotency

The MVP runs in one local process and persists its progress. The queue names below are reserved for later distributed execution; Redis and queue workers are not MVP prerequisites.

```
jobs.discover
jobs.normalize
jobs.score
jobs.packet

applications.ready
applications.execute
applications.retry
applications.needs_input

contacts.enrich
outcomes.process
```

Every application must have an idempotency key derived from:

```
candidate_id + canonical_job_id
```

A worker retry must never accidentally create a second submission record for the same candidate/job pair.

---

# 12. Parallel worktree plan

Eight worktrees are available, with five in the current MVP plan: `core-contracts`, `candidate-brain`, `application-packets`, `browser-ats`, and `dashboard`. Job ingestion, Jev selection, and distributed runtime are parked. WT-05 and WT-06 share the `browser-ats` worktree. The exact ownership and dispatch protocol are in [WORKTREES.md](WORKTREES.md).

## WT-00 — Core Contracts

**Model:** Opus 5.5

**Role:** shared-contract owner under Astra.

Build for the MVP:
- minimal Python package structure and CLI entrypoint in `apps/cli`
- canonical Pydantic contracts for the supplied-URL flow
- SQLite application/event storage and submission idempotency
- state transitions, submission receipts, and artifact references
- configuration conventions, shared fixtures, and contract tests
- `CONTRACTS.md` and executable local verification commands

Core also owns wiring the CLI to the candidate, packet, and browser packages after their interfaces are ready. Zod schemas, Postgres migrations and distributed queue configuration are later work.

Own these interfaces permanently.

### Acceptance criterion
The active worktrees can import the canonical models without redefining them. Local records distinguish a requested application, missing input, confirmed submission, and an uncertain submission; the CLI can wire in the application services without a discovery or scoring dependency.

---

## WT-01 — Job Ingestion

**Model:** Opus 5.5

**Status:** deferred. The browser/CLI handles minimal metadata for the supplied URL in the MVP. The following discovery work is a later milestone.

Build:
- source adapter interface
- job page parser
- normalization
- salary extraction
- location extraction
- ATS detection
- canonical URLs
- deduplication
- job fingerprinting

Interfaces:

```python
async def discover_jobs(query: SearchSpec) -> list[RawJob]
async def normalize_job(raw: RawJob) -> JobPosting
async def dedupe(job: JobPosting) -> DuplicateResult
```

### Acceptance criterion
100 syndicated listings produce canonical jobs and duplicate clusters.

---

## WT-02 — Jev Job Selection / Scoring / Router

**Implementation worker:** Opus 5.5

**Product decision model:** Jev, via TypeSafe AI

**Status:** deferred. The user chooses the job in the MVP; the following selection work is a later milestone.

Build:
- Jev integration for deciding which jobs to apply for
- candidate/job context and versioned selection rubrics
- typed fit assessments and final `APPLY` / `SKIP` / `REVIEW` decision
- candidate-constraint checks and confidence-based review routing
- P0/P1/P2 effort routing for selected jobs and `DROP` for skipped jobs
- decision records with evidence, probabilities, model/rubric versions, and overrides
- candidate-labeled job-selection eval fixtures and provider-failure handling

### Acceptance criterion
50 representative fixture jobs are evaluated against candidate-labeled apply/skip/review decisions, with disagreements reported. Verify that only eligible `APPLY` decisions enter the application pipeline, uncertain cases reach review, provider failures do not enqueue applications, and every decision has an inspectable record.

---

## WT-03 — Candidate Brain

**Model:** Opus 5.5

**MVP scope:** load the user's verified contact details, resume, factual background and saved screening answers. Keep simple provenance for answers and claims. A broader fact graph and automated resume-variant strategy can follow later.

Roadmap responsibilities; limit current work to the MVP scope above:
- structured candidate profile
- candidate fact graph
- provenance
- standard screening answers
- resume variants
- salary/location/work-auth preferences
- claim validation

Interfaces:

```python
get_facts_for_job(job)
get_answerable_fact(question)
validate_generated_claim(text)
select_resume_variant(job)
```

### Acceptance criterion
Generated content cannot introduce unsupported factual or quantitative claims.

---

## WT-04 — Application Packet Generator

**Model:** Opus 5.5

**MVP scope:** resolve the current form's fields from the candidate profile and supplied resume, draft any required text from verified facts, and identify required answers that need user input. This package has no Jev or job-scoring dependency. Elaborate tailoring and optional cover letters are later improvements.

Roadmap responsibilities; limit current work to the MVP scope above:
- deterministic answer resolver
- resume selection/tailoring
- custom question generation
- cover-letter generation
- salary responses
- confidence scoring
- NEEDS_INPUT routing

Decision order:

```
known deterministic answer
    -> answer directly

candidate fact lookup
    -> templated answer

complex written question
    -> LLM

insufficient evidence
    -> NEEDS_INPUT
```

### Acceptance criterion
A representative application fixture resolves known answers and explicitly flags unsupported required questions without inventing candidate facts.

---

## WT-05 — Browser Runtime

**Model:** Opus 5.5

Build:
- Playwright session manager
- DOM inspector
- field resolver
- form planner
- executor
- file upload handling
- validation inspector
- multi-step navigation
- submission detection
- screenshot/debug artifacts

Events:

```
form.discovered
field.unresolved
page.completed
validation.failed
application.submitted
```

### Acceptance criterion
Complete a local mock ATS application containing text, select, radio, checkbox, textarea, and file upload controls.

---

## WT-06 — ATS Adapters

**Model:** Opus 5.5

**MVP scope:** the first user-provided application's ATS, plus local mock forms. Add further adapters after that flow reliably submits and verifies acceptance. The later backlog is:

1. Greenhouse
2. Lever
3. Ashby
4. Workday
5. Rippling
6. SmartRecruiters
7. Generic fallback improvements

### Acceptance criterion
The same `ApplicationPacket` works with the local mock form and the first supported ATS without changing its structure. Broader ATS coverage follows later.

---

## WT-07 — Queue / Distributed Runtime

**Model:** Opus 5.5

**Status:** deferred. Core owns the local CLI and durable application state in the MVP. This worktree later owns hosted/API orchestration and distributed execution.

Build:
- FastAPI endpoints and hosted orchestration that connect the pipeline
- worker queues
- leases
- retries
- timeouts
- idempotency
- dead-letter queues
- worker lifecycle
- concurrency controls
- distributed locks
- recovery tests
- observability

### Acceptance criterion
Kill workers randomly during a 100-job test and recover without corrupting state or creating duplicate application records.

---

## WT-08 — Dashboard / Analytics

**Model:** Opus 5.5

**Status:** active for the supplied-URL MVP. Build the Next.js interface in parallel with the executor: application URL, profile/resume, progress, missing required answers, recovery states, and saved submission receipt. The frontend must use the canonical executor and must never display simulated success for a real application. The local CLI remains available.

The broader dashboard and analytics responsibilities below are deferred until the application flow is reliable.

### Dashboard
- jobs discovered
- qualified
- queued
- submitted
- screens
- interviews
- offers

### Queue view
- P0/P1/P2
- state
- worker
- errors

### Job detail
- JD
- score
- routing reasons
- resume
- answers
- application events

### Human review
- unknown questions
- ambiguous facts
- failed fields
- document requests

### Analytics
Conversion rates by:
- role family
- salary band
- industry
- source
- resume variant
- location
- seniority
- company size

### Acceptance criterion
The frontend can run the supplied-URL flow through the real local executor, resolve missing inputs, and show truthful confirmation, duplicate and uncertain states. Users can understand a failed application and its next required action without opening raw server logs.

---

# 13. Agent roles

## Astra Max — Orchestrator / CTO

Astra owns the **whole system**, not feature implementation.

Responsibilities:
1. Maintain global architectural coherence.
2. Own integration order.
3. Track worktree status.
4. Identify blockers.
5. Review cross-worktree interfaces.
6. Prevent duplicate abstractions.
7. Run integration checks.
8. Assign bugs to the correct worktree.
9. Triage repeated failures and assign a different approach or specialist when needed.
10. Merge only when acceptance criteria are met.

Astra maintains:

```
ARCHITECTURE.md
CONTRACTS.md
WORKTREES.md
INTEGRATION_STATUS.md
```

Astra should not spend its context budget writing routine adapters.

---

## Fable — Optional Specialist / Difficult Problems

Fable is not currently assigned a worktree. Astra owns escalation decisions within the eight-Opus roster and may use a specialist when one is available and assigned.

Use Fable for:
- shared architecture
- scoring logic
- distributed systems
- concurrency
- idempotency
- novel bugs
- long unsupervised investigations
- difficult cross-module refactors

Escalation rule:

```
Opus attempt 1
    -> still broken

Opus attempt 2
    -> still broken

STOP
    -> Astra reviews the evidence and assigns a different approach or specialist
```

Do not allow workers to burn hours repeating failed approaches.

---

## Opus 5.5 — Default Builder

Use Opus for:
- implementation
- feature work
- parsers
- ATS adapters
- Playwright
- tests
- API endpoints
- UI
- schema consumers
- ordinary debugging
- code review inside a bounded worktree

Most code should be produced by Opus workers.

---

## Jev / TypeSafe — Job Selection Decision Maker

In a later milestone, Jev will run inside the product and decide **which jobs to apply for**. WT-02 owns that future integration and selection rubric under Astra's interface review. The current supplied-URL MVP has no Jev dependency.

Its responsibility is evaluating candidate/job fit and choosing `APPLY`, `SKIP`, or `REVIEW`. The selected jobs then pass to the existing candidate, packet-generation, and browser services for preparation and execution.

Record selection decisions alongside later screens, interviews, and offers so the team can improve the job-selection rubric from observed outcomes.

---

# 14. Worktree communication protocol

Every worktree starts by reading:

```
ARCHITECTURE.md
CONTRACTS.md
WORKTREES.md
```

Every worker finishes with:

```yaml
status: complete

changed:
  - ...

public_interfaces_changed:
  - none

tests:
  - ...

dependencies:
  - ...

known_issues:
  - ...

ready_to_merge: true
```

Hard rule:

> No worktree changes another worktree's public interface without Astra approval.

---

## 15. Suggested merge order

Current MVP:

```
WT-00 minimal contracts + local storage + CLI skeleton
    |
    +--> WT-03 candidate brain
    +--> WT-05 browser

WT-00 + WT-03
    -> WT-04 packet generator

WT-05
    -> WT-06 adapter for the first supplied application URL

WT-03 + WT-04 + WT-05/06
    -> WT-00 CLI integration
    -> confirmed submission and local receipt
```

Build the frontend against a narrow service interface in parallel, then connect it to the integrated executor and verify the same submission flow through the UI. Job ingestion, Jev selection, distributed queues and outcome analytics join in later milestones. Integrate the supplied-URL flow continuously rather than waiting for the broader roadmap.

---

# 16. Repository layout

Target layout across milestones; create only the packages needed by the current MVP.

```
interviewmaxxing/
├── apps/
│   ├── cli/
│   ├── api/
│   └── web/
├── packages/
│   ├── core/
│   ├── candidate/
│   ├── ingestion/
│   ├── scoring/
│   ├── generation/
│   ├── browser/
│   ├── ats/
│   └── analytics/
├── workers/
│   ├── ingest/
│   ├── score/
│   ├── apply/
│   └── outcomes/
├── adapters/
│   ├── greenhouse/
│   ├── lever/
│   ├── ashby/
│   ├── workday/
│   ├── rippling/
│   └── generic/
├── prompts/
├── evals/
├── fixtures/
├── migrations/
├── docker/
└── docs/
```

---

# 17. First integration milestone

> **One user-provided application URL -> one submitted application -> verified confirmation and a saved receipt.**

Definition of done:

```
User's application URL + verified profile/resume
 -> application request recorded
 -> job identity and prior submission checked
 -> form parsed
 -> factual packet prepared
 -> browser fills fields and uploads documents
 -> missing required answers resolved if needed
 -> application submitted
 -> site confirmation verified
 -> receipt and event history saved
```

The receipt identifies the job, application URL, submission time, and available confirmation reference or evidence. Until confirmation is observed, report the actual blocked or uncertain state.

Verify missing-answer resume, duplicate prevention, and ambiguous submission recovery using local fixtures. Verify the complete frontend flow against the same real local executor and fixture server, including persisted state and server-side submission counts. Then verify the supported real application flow using the user's supplied URL and information. Job discovery, Jev selection, distributed queues and analytics are outside this acceptance criterion.

---

# 18. Later milestones — Discovery, Jev selection and interview feedback

After URL-based application submission is reliable, add job discovery and Jev's job-selection decisions. They feed selected jobs into the same application executor. Outcome ingestion and ranking improvements follow as the product grows.

After application execution is reliable, add outcome ingestion:

```
application
 -> recruiter response
 -> screen
 -> hiring manager
 -> final
 -> offer
```

Measure:

```
application -> screen rate
screen -> hiring-manager rate
hiring-manager -> final rate
final -> offer rate
```

Segment by:
- role family
- seniority
- source
- salary band
- resume variant
- location
- outreach strategy

The ranking system should eventually learn from these outcomes.

---

# 19. Product principle

Interviewmaxxing should not optimize for the vanity metric:

```
applications/day
```

It should optimize for:

```
interviews/week
offers/month
expected offer value
```

**Applications are inventory. Interviews are the conversion. Offers are revenue.**
