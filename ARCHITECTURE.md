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

---

## 2. MVP

The first end-to-end milestone is:

```bash
interviewmaxxing apply <job-url>
```

That command should:

1. Fetch the job.
2. Normalize company, title, location, salary, description, and ATS.
3. Score the job against the candidate profile.
4. Select the best resume variant.
5. Open and inspect the application form.
6. Convert the live form into a normalized `ApplicationForm`.
7. Build a truthful `ApplicationPacket`.
8. Fill the application.
9. Surface unresolved or ambiguous questions.
10. Require human confirmation where needed.
11. Submit.
12. Persist the full application event history.

Do this for **one URL correctly** before optimizing for large-scale concurrency.

---

## 3. Core architecture

```
                         +---------------------------+
                         |       CONTROL PLANE       |
                         | goals / rules / budgets   |
                         +-------------+-------------+
                                       |
                                       v
+-------------+   +-----------+   +---------+   +---------+
| Job Sources |-->| Normalize |-->| Dedupe  |-->| Scoring |
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

---

## 4. Technical stack

### Backend / orchestration
- **Python 3.12+**
- **FastAPI**
- **Pydantic**
- **PostgreSQL**
- **Redis**
- Queue runtime: start lightweight; use Dramatiq/Celery or Temporal only if needed.

### Browser runtime
- **Playwright**
- DOM-first inspection.
- Browser agents execute plans; they should not own career strategy.

### Frontend
- **Next.js**
- TypeScript
- Dashboard for queue, review, analytics, failures, and outcomes.

### Models
- **GPT Astra**: project orchestrator / integration manager.
- **Fable 5.x**: architecture, scoring, distributed systems, difficult investigations.
- **Opus 5.5**: default implementation worker for bounded feature work.
- **TypeSafe / cheap local classifiers**: high-volume typed routing and scoring.
- Optional local/open-source models for cheap generation and classification.

---

## 5. Canonical domain objects

These contracts belong to the core worktree and must not be independently redefined by other agents.

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

### JobMatch

```python
class JobMatch:
    job_id: str
    candidate_id: str
    role_family: str
    seniority: str
    fit_score: float
    interview_probability: float | None
    confidence: float
    priority: Literal["P0", "P1", "P2", "DROP"]
    resume_variant: str
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
    job_id: str
    candidate_id: str
    state: str
    packet_id: str | None
    submitted_at: datetime | None
    failure_reason: str | None
```

---

## 6. Application state machine

```
DISCOVERED
  -> NORMALIZED
  -> SCORED
  -> PACKET_READY
  -> QUEUED
  -> EXECUTING
  -> NEEDS_INPUT
  -> SUBMITTED
  -> REJECTED
  -> RECRUITER_SCREEN
  -> HIRING_MANAGER
  -> FINAL_ROUND
  -> OFFER
```

Failure states:

```
FAILED_RETRYABLE
FAILED_PERMANENT
DUPLICATE
WITHDRAWN
```

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

## 8. Job scoring and routing

Do not ask one model:

> "Is this a good job?"

Ask atomic questions and combine the outputs in code.

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
likely_interview_probability
best_resume_variant
human_outreach_value
```

Initial routing:

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

Example ranking function:

```python
priority_score = (
    0.30 * experience_match
    + 0.20 * seniority_match
    + 0.20 * compensation_match
    + 0.15 * technical_ai_match
    + 0.10 * leadership_match
    + 0.05 * location_match
)
```

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

Initial adapters:

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

## 11. Queues

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

## WT-00 — Core Contracts

**Model:** Fable 5.x  
**Role:** architecture owner under Astra.

Build:
- monorepo structure
- canonical Pydantic/Zod schemas
- Postgres schema
- migrations
- event model
- state machine
- queue names
- configuration conventions
- fixtures
- contract tests

Own these interfaces permanently.

### Acceptance criterion
All other worktrees can import the canonical models without redefining them.

---

## WT-01 — Job Ingestion

**Model:** Opus 5.5

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

## WT-02 — Scoring / Router

**Model:** Fable 5.x

Build:
- typed scoring primitives
- TypeSafe integration
- local-model decision provider
- scoring formula
- confidence routing
- P0/P1/P2/DROP
- eval fixtures

### Acceptance criterion
50 representative fixture jobs produce stable, explainable routing decisions.

---

## WT-03 — Candidate Brain

**Model:** Opus 5.5

Build:
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

Build:
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
A 50-question fixture application can be completed with unsupported questions explicitly flagged.

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

Build adapters in this order:

1. Greenhouse
2. Lever
3. Ashby
4. Workday
5. Rippling
6. SmartRecruiters
7. Generic fallback improvements

### Acceptance criterion
The same `ApplicationPacket` can traverse multiple ATS fixtures without changing packet structure.

---

## WT-07 — Queue / Distributed Runtime

**Model:** Fable 5.x

Build:
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

Build Next.js control plane.

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
Any failed application can be diagnosed from the dashboard without opening raw server logs.

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
9. Decide when a task needs escalation to Fable.
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

## Fable — Architect / Difficult Problems

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
    -> Fable investigation
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

## Cheap Decision Models / TypeSafe

Use these in the **product**, not as coding agents.

Purpose:
- high-volume classification
- role-family detection
- salary likelihood
- fit scoring
- seniority
- routing
- confidence estimation
- cheap job triage

Expensive models should only receive jobs/tasks that justify expensive reasoning.

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

```
WT-00 core
    |
    +--> WT-01 ingestion
    +--> WT-02 scoring
    +--> WT-03 candidate brain
    +--> WT-05 browser
    +--> WT-07 queue

WT-02 + WT-03
    -> WT-04 packet generator

WT-05
    -> WT-06 ATS adapters

all canonical events/models
    -> WT-08 dashboard
```

Do not wait for every worktree to finish before integrating. Merge vertical slices continuously.

---

# 16. Repository layout

```
interviewmaxxing/
├── apps/
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

The first milestone is not "lots of agents running."

It is:

> **One job URL -> one correctly prepared, inspectable application.**

Definition of done:

```
URL
 -> job scraped
 -> normalized
 -> deduplicated
 -> scored
 -> resume selected
 -> form parsed
 -> packet generated
 -> browser fills form
 -> unknown fields surfaced
 -> human confirms
 -> submission recorded
```

Once this works reliably, increase worker concurrency.

---

# 18. Second milestone — Interview feedback loop

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
