# Interviewmaxxing contracts

Owner: **core-contracts** (WT-00). Contract version `2` (`interviewmaxxing_core.CONTRACT_VERSION`). Version 2 is the C1R review revision: form/question identity, choice validity, saved-answer scope and fact verification.
Scope: the supplied-URL MVP (ARCHITECTURE.md §2 and §17), including the user-activated frontend through the same Python executor and state. Discovery, Jev selection, distributed queues and outcome analytics are out of scope.

Downstream packages **import** these types; they never redeclare, subclass-to-extend, or copy them. A needed change is a request to core (see [Change requests](#change-requests)).

## 1. Package layout and import paths

The repository is a [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/). The root `pyproject.toml` is virtual (not a package); every directory `packages/*` or `apps/*` containing a `pyproject.toml` is a member.

| Directory | Distribution | Import package | Owner | Status |
| --- | --- | --- | --- | --- |
| `packages/core` | `interviewmaxxing-core` | `interviewmaxxing_core` | core-contracts | implemented |
| `apps/cli` | `interviewmaxxing-cli` | `interviewmaxxing_cli` (script `interviewmaxxing`) | core-contracts | skeleton; I1 integrates |
| `packages/candidate` | `interviewmaxxing-candidate` | `interviewmaxxing_candidate` | candidate-brain | reserved |
| `packages/generation` | `interviewmaxxing-generation` | `interviewmaxxing_generation` | application-packets | reserved |
| `packages/browser` | `interviewmaxxing-browser` | `interviewmaxxing_browser` | browser-ats | reserved |
| `packages/ats` | `interviewmaxxing-ats` | `interviewmaxxing_ats` | browser-ats | reserved (optional; adapters may live in `browser`) |

Tests live in `tests/<package>/` (e.g. `tests/candidate/`), fixtures in `tests/fixtures/<package>/`. `tests/conftest.py` (core-owned) is shared by all.

### Adding your package (downstream workers)

Create `packages/<name>/pyproject.toml` and `packages/<name>/src/interviewmaxxing_<name>/__init__.py`:

```toml
[project]
name = "interviewmaxxing-<name>"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["interviewmaxxing-core"]        # plus your own, e.g. "playwright>=1.47"

[tool.uv.sources]
interviewmaxxing-core = { workspace = true }

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/interviewmaxxing_<name>"]
```

**Root dependencies and `uv.lock` belong to core/coordinator** (WORKTREES.md). Downstream workers must not edit or commit the root `pyproject.toml`, `uv.lock`, `tests/conftest.py` or `scripts/verify.sh`. The glob membership needs no root edit. For targeted checks in your worktree, use a task-local environment without touching the lock:

```bash
uv venv .venv-task && . .venv-task/bin/activate
uv pip install -e packages/core -e packages/<name> pytest   # plus your own test deps
python -m pytest tests/<name>
```

Report every third-party dependency (name and version range) you need as a dependency request in your handoff. The coordinator/core adds it and regenerates the shared `uv.lock` at integration; `scripts/verify.sh` (locked install, `ruff`, `mypy --strict` over every workspace member, full tests) is then the acceptance check.

## 2. Core module map

Everything below is re-exported from `interviewmaxxing_core`.

| Module | Contents |
| --- | --- |
| `forms` | `SemanticType`, `ControlType`, `FieldOption`, `ApplicationField`, `ApplicationForm`, `FormScope`, `normalize_text`, `EXPLICIT_ANSWER_REQUIRED`, `PROTECTED_ATTRIBUTE_TYPES`, `PROFILE_IDENTITY_TYPES` |
| `candidate` | `CandidateProfile`, `CandidateIdentity`, `PostalAddress`, `CandidateFact`, `FactVerification`, `VerificationStatus`, `VerificationMethod`, `ResumeArtifact`, `SavedAnswer`, `AnswerScope`, `Experience`, `Education` |
| `artifacts` | `ArtifactRef`, `EvidenceRef`, `EvidenceKind`, `sha256_file` |
| `jobs` | `JobIdentityObservation`, `IdentityEvidenceKind`, `JobRecord` |
| `packets` | `ApplicationPacket`, `PacketAnswer`, `Provenance`, `AnswerSource`, `AnswerValue` (`TextValue`/`ChoiceValue`/`MultiChoiceValue`/`BooleanValue`/`FileValue`), `MissingInput`, `MissingReason`, `UserInput`, `AnswerReuse`, `answer_problems`, `provenance_problems` |
| `execution` | `PageInspection`, `PageKind`, `FillResult`, `FieldFillResult`, `FieldFillStatus`, `NavigationResult`, `SubmitActionResult`, `SubmissionObservation`, `SubmissionOutcome`, `NotSubmittedNext`, `SubmissionReconciliation`, `ReconciliationMethod` |
| `applications` | `ApplicationState`, `TRANSITIONS`, state sets, `ApplicationRequest`, `Application`, `ApplicationEvent`, `Claim`, `SubmissionAttempt`, `Receipt`, `RequestDisposition` |
| `interfaces` | service `Protocol`s (§6) and `PacketContext`, `BrowserOptions`, `ApplyOutcome` |
| `store` | `ApplicationStore`, `RequestResult`, `BindResult` |
| `errors` | `StoreError`, `NotFound`, `InvalidTransition`, `ClaimUnavailable`, `ClaimLost`, `SubmissionBlocked`, `IdentityConflict` |
| `urls` | `normalize_application_url`, `InvalidApplicationUrl` |
| `discovery` | D0 job discovery/selection/pipeline contracts (§11): `JobSearchQuery`, `OnsiteTarget`, `RemoteTarget`, `CompensationFloor`, `SourceSearchResult`, `JobSearchRun`, `JobListing`, `ListingSource`, `Compensation`, `SelectionPreferences`, `JobSelection`, `ModelDecision`, `PolicyHold`, `PipelineEntry`, `PipelineStages`, helpers `listing_id_for`, `employer_job_key`, `meets_floor`, `snapshot_hash` |
| `config` | `LocalPaths` |

All contracts derive from `Contract`: Pydantic v2, **frozen** (use `model_copy(update=...)`), **extra fields forbidden**, datetimes **timezone-aware and normalized to UTC** (naive datetimes are rejected). Every contract serializes with `model_dump_json()` / `model_validate_json()` and publishes `model_json_schema()`.

## 3. Candidate data (candidate-brain produces)

`CandidateProfile(id, identity, resume, facts, saved_answers, experience, education)`

- `CandidateIdentity` — verified contact details; `verified_at` is **required**.
- `CandidateFact(id, key, value, source, verification, confidence, evidence)`. `verification: FactVerification(status, method, verified_at)` is **required, with no default**:
  - `VERIFIED` requires `method` (`USER_STATED`: the user entered it; `USER_CONFIRMED`: extracted, e.g. from the resume, then explicitly confirmed by the user) and `verified_at`.
  - `UNVERIFIED` must carry neither (for example, an extracted fact awaiting confirmation).
  - `confidence` is extraction confidence only. **It is never proof**: `confidence=1.0` with `UNVERIFIED` is still unverified. `CandidateFact.is_verified`, `CandidateProfile.verified_facts()` and `verified_only()` are the only verification tests.
- `ResumeArtifact(ArtifactRef)` — the supplied resume: absolute local `path`, `sha256`, `size_bytes`, `media_type`, `variant="supplied"`, optional `extracted_text`. `verify()` rechecks the file digest.
- `SavedAnswer(id, scope, job_identity_key, job_url, employer, semantic_type, question, match_phrases, value, confirmed_at)` — an answer the user explicitly saved for reuse. `value` is in the user's terms (`"Yes"`, `False`, list of labels). Mapping it to a form's option values is the resolver's job, and ambiguity must be reported, not guessed. `scope: AnswerScope` is **required**:
  - `GLOBAL` — explicitly reusable for any job; must not name a job or employer.
  - `JOB` — only for one job; needs `job_identity_key` (preferred; `JobIdentityObservation.identity_key`) or `job_url` (stored normalized). `employer` is informational only.
  - `SavedAnswer.applies_to(job)`: GLOBAL always applies. A JOB answer with an identity key applies only to a job bound to that identity (not to an unbound job); otherwise it applies only to the same normalized URL. Nothing is matched by employer name, so an answer about one employer is never promoted to another. Use `CandidateProfile.saved_answers_for(type, job=...)` / `applicable_saved_answers(job)`.
- Experience/education `fact_ids` must reference existing facts (validated). Answers cite fact ids, never experience/education ids.

**Loading contract (`CandidateLoader`).** The user supplies identity with `verified_at`, the resume file, facts each with an explicit verification, and saved answers each with an explicit scope. The loader validates the schema, checks that the resume exists and its digest matches (or computes it), and checks reference integrity. It never fabricates data, never upgrades `UNVERIFIED` to `VERIFIED`, never sets `verified_at` itself, and never widens a scope. It may return unverified facts (so the user can be asked to confirm them); resolvers must use verified facts only, which `provenance_problems` enforces.

## 4. Forms, packets and answers

### Fields

`ApplicationField(id, label, semantic_type, control_type, selector, required, input_type, options, accept, max_length, placeholder, help_text, validation_error)`
`ApplicationForm(url, ats_type, step, fields, is_final_step, submit_selector, next_selector, page_errors, inspected_at)`

- `id` is stable within the form and is the answer key. Packets never address selectors.
- `FieldOption(value, label, selector, disabled)`: **`value` is the machine value** the page submits and the one a packet selects; **`label` is the user-visible text**. Choice controls require options with unique values; non-choice controls must not have options; `accept` is FILE-only.
- `SemanticType` follows ARCHITECTURE.md §7, plus `FULL_NAME`, `PREFERRED_NAME`, `COUNTRY`, `LOCATION`, `CURRENT_COMPANY`, `CURRENT_TITLE`, `START_DATE`, `RELOCATION`, `REFERRAL_SOURCE`, `EEO_*`, `PRONOUNS`, `CONSENT`, `ATTESTATION`, `CUSTOM_TEXT`, `UNKNOWN`.

**Identity.**
- `ApplicationForm.scope` → `FormScope(url, step)`, where `url` is normalized with `normalize_application_url` (tracking parameters do not change it) and `.key` is `"<url>|step=<n>"`.
- `ApplicationField.fingerprint` is the SHA-256 of the complete question the user sees: the normalized label, `help_text` and `placeholder` (`normalize_text`: case-folded, whitespace collapsed), the control type, and sorted `(value, normalized label)` options. Any wording change is a new question. For example, an "I agree" checkbox whose help text changes from certifying accuracy to certifying never having been dismissed gets a new fingerprint, so an earlier answer, missing-input item or packet cannot authorize it. Selector, requiredness, validation messages, input type and inspection time are excluded: re-inspecting the same question (or one whose only change is becoming required or optional) still matches, and requiredness is enforced separately by `answer_problems`/`problems_against`.
- **Browser inspectors (C4)** must put all instruction or attestation text that belongs to a field (`aria-describedby` text, adjacent description, legend text beyond the label) into `label` or `help_text`. Text that is not captured cannot be part of question identity.
- `ApplicationForm.fingerprint` is the SHA-256 of the scope plus every `(field id, field fingerprint)` pair.
- A question is therefore identified by **(form scope, field id, field fingerprint)**. The same `question_0` on two steps, or a changed question reusing an id, are different questions.
- **Question wording (the one renderer).** `render_question(label, help_text=None, placeholder=None) -> str` and `ApplicationField.question_text` (= `render_question(field.label, field.help_text, field.placeholder)`) in `interviewmaxxing_core.forms`, re-exported from `interviewmaxxing_core` with `QUESTION_PART_SEPARATOR`:
  - Parts appear in the fixed order **label, help text, placeholder**. Each part is trimmed and its internal whitespace runs collapse to one space. Empty or whitespace-only parts are omitted. The remaining parts are joined with `QUESTION_PART_SEPARATOR` (`"\n"`).
  - Nothing else changes. Case, punctuation, comparison signs, currency and units (`<`, `>`, `$`, `€`, `%`) are kept verbatim, and no decoration such as `placeholder:` is added. `normalize_text` therefore treats the separator as one space, so `normalize_text(question_text)` equals the three parts joined by spaces.
  - It covers exactly the text in `fingerprint`. Examples: `"I agree\nI certify that all application information is accurate."` and `"Expected salary\n€"`.
- **Carriers.** `MissingInput.for_field` puts `field.question_text` in `MissingInput.label` (the user-facing wording). `UserInput.for_field` puts it in `UserInput.question`, and `UserInput.answering(missing, ...)` copies `missing.label` there. `UserInput.to_saved_answer` copies `question` to `SavedAnswer.question`. An answer given for "I agree" plus help text is therefore saved under that complete wording, not under "I agree". No schema changed, and matching and provenance rules are unchanged.
- **Integration.**
  - Generation (C3) should compare a saved answer's `question`/`match_phrases` with `field.question_text`, applying one comparison normalization to both sides. That normalization must keep meaning-bearing symbols.
  - UIs (CLI/F2) display `MissingInput.label` as multi-line text and must not rebuild wording from the field label.
  - Producers must build items with `MissingInput.for_field`/`UserInput.for_field` rather than setting `label`/`question` by hand.

| `ControlType` | Answer value | Notes |
| --- | --- | --- |
| `TEXT` | `TextValue(text)` | `input_type` = HTML type (email, tel, url, number, date) |
| `TEXTAREA` | `TextValue(text)` | `max_length` enforced |
| `SELECT` | `ChoiceValue(value, label)` | single choice |
| `RADIO` | `ChoiceValue(value, label)` | per-option `selector` |
| `MULTISELECT` | `MultiChoiceValue(choices: list[FieldOption])` | |
| `CHECKBOX_GROUP` | `MultiChoiceValue(choices)` | several checkboxes, one question |
| `CHECKBOX` | `BooleanValue(checked)` | a required checkbox must be checked |
| `FILE` | `FileValue(artifact: ArtifactRef)` | must satisfy `accept` (`.ext`, `type/*` or exact media type) |
| `UNSUPPORTED` | none | report as `MissingInput(reason=UNSUPPORTED_CONTROL)` |

`answer_problems(field, value) -> list[str]` checks a value against the **actual** field. Every chosen option (single or multi) must exist, be **enabled**, have a non-empty value (placeholders such as `value=""` "Select..." are never answers, whether disabled or not), and its answer `label` must equal the option's label after `normalize_text` (so `value="US", label="Canada"` is rejected when `US` is "United States"). Multi-choice values may not repeat, and a required multi-choice needs at least one. Text is checked against `max_length` and must be non-blank when required.

### Packets (application-packets produces)

`ApplicationPacket(id, application_id, job_id, candidate_id, form_url, form_step, form_fingerprint, resume_variant, answers, missing_inputs, cover_letter, created_at)`; properties `scope`, `unresolved_fields`, `is_complete`; `answer_for(field_id)`. `form_fingerprint` is the `ApplicationForm.fingerprint` of the inspection the packet answers.

`PacketAnswer(field_id, semantic_type, value, provenance, confidence)` with `Provenance(source, reference_ids, note)`:

| `AnswerSource` | `reference_ids` | Allowed for |
| --- | --- | --- |
| `PROFILE_IDENTITY` | optional | only `PROFILE_IDENTITY_TYPES` (name, email, phone, address parts, country, location, LinkedIn/website/GitHub) |
| `CANDIDATE_FACT`, `GENERATED_FROM_FACTS` | fact ids (required) | facts must exist and be **verified** |
| `SAVED_ANSWER` | saved-answer ids (required) | must `applies_to(job)`; its `semantic_type`, if set, must equal the answer's |
| `RESUME` | exactly `[candidate.resume.id]` | the `FileValue` digest must equal the supplied resume's |
| `USER_INPUT` | `UserInput.id` (required) | same question (scope, field id, fingerprint) and the same value the user gave |

**Never infer.** Work authorization, sponsorship, salary, consent, attestation, EEO/protected attributes and pronouns (`EXPLICIT_ANSWER_REQUIRED`) may be answered only from `SAVED_ANSWER` or `USER_INPUT`. Without one, emit a `MissingInput`. This is enforced twice: `PacketAnswer` rejects it at construction based on its own `semantic_type`, and `problems_against` rejects it based on the **inspected field's** type, so a mislabelled answer cannot slip through.

Validation, all returning `list[str]` (empty means valid):
- `packet.problems_against(form)` — the packet's scope equals `form.scope` (else that is the only problem reported) and its `form_fingerprint` equals `form.fingerprint`. Every answer targets an existing field, and **its `semantic_type` equals the field's**. The field's actual type is checked for explicit-answer and identity-source permissions, then `answer_problems`. Every missing input is a question on this form (`MissingInput.matches`), and every required field is answered or reported missing.
- `provenance_problems(packet, form=, candidate=, job=, user_inputs=)` — the provenance rules in the table above, plus packet candidate and job ids.
- `PacketContext.problems(packet)` — both of the above plus the application id. **The runner rejects any packet for which this is non-empty.**

`MissingInput(id, field_id, form_url, form_step, field_fingerprint, label, reason, prompt, semantic_type, control_type, options, required, candidates)`. Build field items with `MissingInput.for_field(form, field, reason=, prompt=)`. A field item must carry `form_url`, `form_step` and `field_fingerprint`; only `USER_ACTION` (sign-in/CAPTCHA) may omit the field and its scope. `reason` ∈ `NO_ANSWER`, `EXPLICIT_ANSWER_REQUIRED`, `UNCOVERED_ATTESTATION`, `AMBIGUOUS`, `UNSUPPORTED_CONTROL`, `USER_ACTION`.

`UserInput(id, form_url, form_step, field_id, field_fingerprint, question, semantic_type, value, reuse, provided_at)` — the user's answer to one question on one step. Create it with `UserInput.answering(missing_input, value)` (checked against the item's options) or `UserInput.for_field(form, field_id, value)` (checked with `answer_problems`). `matches(form)` is true only for the same scope, field id and fingerprint.

`reuse: AnswerReuse` defaults to **`APPLICATION`: the answer stays local to this application.** Only `JOB` or `GLOBAL`, chosen by the user, produce a `SavedAnswer` via `to_saved_answer(job=)`. `JOB` binds it to that job's identity key (or its normalized URL when unbound). The candidate package persists it through `SavedAnswerWriter`. File answers are never saved.

## 5. Browser results and job identity (browser-ats produces)

- `PageInspection(kind, observed_url, form, job_identity, message, evidence, inspected_at)`; `form` is present **iff** `kind == APPLICATION_FORM`. `PageKind` also covers `JOB_DESCRIPTION`, `SIGN_IN_REQUIRED`, `CAPTCHA` (`USER_ACTION_PAGES`), `CONFIRMATION`, `ALREADY_APPLIED`, `JOB_CLOSED`, `ERROR`, `UNKNOWN`.
- `JobIdentityObservation(ats_type, ats_tenant, external_job_id, evidence_kind, evidence, observed_url, company, title, location, observed_at)`; `identity_key = "ats:<type>:<tenant>:<job id>"` (lower-cased). `IdentityEvidenceKind` is `ATS_JOB_ID_ON_PAGE`, `STRUCTURED_DATA` or `USER_CONFIRMED`. **There is no redirect kind**: arriving at a URL via redirect proves nothing about job identity, and `observed_url` is recorded but never bound as an alias.
- `FillResult(form_step, fields: list[FieldFillResult], page_errors, evidence)`; `.ok`; statuses `FILLED`, `SKIPPED`, `FAILED`, `VERIFICATION_MISMATCH`.
- `NavigationResult(advanced, inspection, validation_errors)` — non-final steps only.
- `SubmitActionResult(dispatched, dispatched_at, detail)` — says nothing about acceptance. `dispatched=False` only if certain nothing reached the site.
- `SubmissionObservation(outcome, signals, confirmation_reference, observed_url, validation_errors, evidence, next_state, detail, observed_at)`:
  - `ACCEPTED` requires ≥1 concrete acceptance signal (confirmation page/text/reference). A click, navigation or timeout is **not** acceptance.
  - `NOT_SUBMITTED` requires proof (`signals` or `validation_errors`) and `next_state` ∈ `FILLING`, `NEEDS_INPUT`, `FAILED_RETRYABLE`, `FAILED_PERMANENT`.
  - `UNKNOWN` for everything else.
- `EvidenceRef(id, kind, path, uri, sha256, description, captured_at)`; `path` is a relative POSIX path under the artifacts root, conventionally `<application_id>/<file>`; absolute paths and `..` are rejected.

## 6. Service interfaces

All in `interviewmaxxing_core.interfaces`, `@runtime_checkable` `Protocol`s. Browser-facing and packet interfaces are `async`.

```python
class CandidateLoader(Protocol):                               # candidate-brain
    def load(self, candidate_id: str) -> CandidateProfile: ...
    # raises CandidateNotFound / CandidateProfileInvalid; see "Loading contract" (§3)

class SavedAnswerWriter(Protocol):                             # candidate-brain
    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None: ...
    # persists UserInput.to_saved_answer(...) results with their scope unchanged

@dataclass(frozen=True)
class PacketContext:
    application: Application
    job: JobRecord                 # must be application.job_id
    form: ApplicationForm          # the current inspection
    candidate: CandidateProfile    # must be application.candidate_id
    user_inputs: Sequence[UserInput] = ()   # store.get_user_inputs(app_id, form)
    # __post_init__ raises ValueError if any user input does not match(form)
    def problems(self, packet: ApplicationPacket) -> list[str]: ...

class PacketResolver(Protocol):                                # application-packets
    async def resolve(self, context: PacketContext) -> ApplicationPacket: ...
    # result must satisfy context.problems(packet) == []

@dataclass(frozen=True)
class BrowserOptions:
    artifacts_dir: Path            # LocalPaths.application_artifacts(app_id)
    artifacts_root: Path           # LocalPaths.artifacts_dir (EvidenceRef.path base)
    profile_dir: Path | None = None  # LocalPaths.browser_dir (persistent sign-in)
    headless: bool = False
    slow_mo_ms: int = 0

class BrowserSessionFactory(Protocol):                         # browser-ats
    async def start(self, options: BrowserOptions) -> ApplicationBrowser: ...

class ApplicationBrowser(Protocol):                            # browser-ats
    async def open(self, url: str) -> PageInspection: ...
    async def inspect(self) -> PageInspection: ...
    async def fill(self, form: ApplicationForm, packet: ApplicationPacket) -> FillResult: ...
                                                         # raise if packet.problems_against(form)
    async def advance(self) -> NavigationResult: ...     # must refuse a submitting action
    async def submit(self) -> SubmitActionResult: ...    # only after begin_submission
    async def confirm(self) -> SubmissionObservation: ...
    async def wait_for_user(self, reason: str, timeout_s: float | None = None) -> PageInspection: ...
    async def close(self) -> None: ...

class ATSAdapter(Protocol):                                    # browser-ats, internal
    name: str
    async def detect(self, page: Any) -> bool: ...
    async def inspect(self, page: Any) -> PageInspection: ...
    async def fill(self, page: Any, form: ApplicationForm, packet: ApplicationPacket) -> FillResult: ...
    async def next(self, page: Any) -> NavigationResult: ...
    async def submit(self, page: Any) -> SubmitActionResult: ...
    async def detect_submission(self, page: Any) -> SubmissionObservation: ...

class UserInteraction(Protocol):                               # apps/cli (I1)
    async def request_inputs(self, missing: Sequence[MissingInput]) -> Sequence[UserInput]: ...
    async def request_action(self, message: str) -> bool: ...
    async def progress(self, message: str) -> None: ...

class ApplicationRunner(Protocol):                             # apps/cli (I1)
    async def apply(self, application_url: str, *, candidate_id: str) -> ApplyOutcome: ...
    async def resume(self, application_id: str) -> ApplyOutcome: ...
```

`ApplyOutcome(application_id, state, receipt, missing_inputs, message)`; `.submitted` is true only for `SUBMITTED` with a receipt.

**Services observe; the runner decides.** No service writes application state. Browser/packet code returns observations; the runner records them through the store.

## 7. Application state and the store

### States

```
REQUESTED -> INSPECTING -> PACKET_READY -> FILLING -> SUBMITTING -> SUBMITTED
                 ^              |             |
                 +--------------+-------------+   (next page: FILLING -> INSPECTING)
NEEDS_INPUT -> INSPECTING        (resume re-inspects the current page)
SUBMITTING -> SUBMISSION_UNKNOWN -> SUBMITTED | FAILED_RETRYABLE   (reconciliation only)
FAILED_RETRYABLE -> INSPECTING
terminal: SUBMITTED, FAILED_PERMANENT, DUPLICATE, WITHDRAWN
```

The exact table is `interviewmaxxing_core.TRANSITIONS`. Sets: `PRE_SUBMISSION_STATES`, `SUBMISSION_BLOCKING_STATES` (`SUBMITTING`, `SUBMITTED`, `SUBMISSION_UNKNOWN` — never retried), `TERMINAL_STATES`. Transitions into or out of the blocking states happen only through the dedicated submission operations.

### Store API (`ApplicationStore`)

```python
ApplicationStore.open(path, *, clock=utc_now, busy_timeout=30.0)   # context manager; one per thread/process

record_request(candidate_id, application_url, *, requested_at=None) -> RequestResult
    # RequestResult(request, application, job, disposition); .may_proceed
    # disposition: NEW | RESUMABLE | ALREADY_SUBMITTED | SUBMISSION_IN_PROGRESS | SUBMISSION_UNKNOWN | CLOSED
claim(application_id, owner, *, ttl=5min) -> Claim              # ClaimUnavailable if held
renew(claim, *, ttl=5min) -> Claim;  release(claim) -> None
transition(claim, to_state, *, reason=None, failure_reason=None, metadata=None) -> Application
bind_job_identity(claim, observation: JobIdentityObservation) -> BindResult
    # BindResult(job, application, merged_from_job_id, duplicate_of, moved_application_ids)
save_packet(claim, packet) -> Application;  latest_packet(app_id);  get_packet(packet_id)
save_user_inputs(claim, inputs)            # stored under (form scope, field id, fingerprint)
get_user_inputs(app_id, form) -> list[UserInput]   # latest per question on this form, fingerprint-checked
list_user_inputs(app_id) -> list[UserInput]        # latest per (scope, field id), all steps; display only
append_event(claim, event, metadata=None) -> ApplicationEvent    # e.g. form.discovered
add_evidence(claim, evidence, *, attempt_id=None)
begin_submission(claim, *, packet_id=None, lease=10min) -> SubmissionAttempt
record_submission_outcome(claim, attempt_id, observation: SubmissionObservation) -> Application
reconcile_submission(claim, reconciliation: SubmissionReconciliation) -> Application
recover_interrupted_submissions() -> list[Application]
get_application, get_job, find_application(candidate_id, url), list_applications(candidate_id=, states=),
list_requests, list_events, list_attempts, list_evidence, get_receipt(app_id) -> Receipt | None
```

Guarantees (each operation is one `BEGIN IMMEDIATE` transaction; WAL, `synchronous=FULL`):

- **One application per (candidate, canonical job)**, enforced by a UNIQUE constraint. A repeated request (same normalized URL, or an alias already bound) returns the existing application with its disposition and an `application.request_repeated` event.
- **URL normalization** (`normalize_application_url`) lower-cases scheme/host, drops default ports, one trailing slash and known tracking parameters (`utm_*`, `gclid`, `fbclid`, `gh_src`, `lever-source`, …); **all other query parameters are preserved** and sorted by name; fragments are kept only for client routes (`#/…`). Navigation always uses the URL exactly as supplied.
- **Identity binding** (`bind_job_identity`): binding an ATS identity already bound to another job merges this job into that canonical job, repoints its URL aliases and moves its applications; if the candidate already has an application there, this pre-submission application becomes `DUPLICATE` (`duplicate_of` = survivor). A job cannot take a second, different identity, and a merge never rewrites an application whose submission started (`IdentityConflict`).
- **Claims**: mutating operations need the current, unexpired `Claim` (`ClaimLost` otherwise). Terminal transitions release the claim.
- **Events**: every transition writes exactly one event in the same transaction (`application.<state>`), plus `job.identity_bound`, `job.merged`, `packet.saved`, `input.received`, `evidence.recorded`, `application.request_repeated`. Events are append-only (SQL triggers). Prefixes `application.`, `job.`, `submission.`, `packet.`, `input.` are reserved for the store; `append_event` accepts others (`form.discovered`, `field.unresolved`, `page.completed`, `validation.failed`).
- **Submission**: `begin_submission` requires `FILLING`, durably writes `SUBMITTING` plus an open attempt and extends the claim ≥ 10 min, **before** the caller clicks. At most one open attempt per application (partial unique index). `SUBMITTED` is only reachable via an `ACCEPTED` observation or reconciliation, writes a `Receipt`, and is final (SQL trigger).
- **User inputs** are keyed by (form scope, field id). The latest answer per key wins; `get_user_inputs(app_id, form)` returns only answers whose scope is `form.scope` and whose fingerprint still matches the field. An answer to a changed question is never reused (the question is asked again). Schema v2 migrates a v1 database by adding the scope/fingerprint columns; unscoped v1 rows are ignored. The C1 skeleton never wrote inputs or packets.
- **Crash / uncertainty**: if the owner of a `SUBMITTING` application disappears (claim lapses or is released), the next `claim()` or `recover_interrupted_submissions()` marks it `SUBMISSION_UNKNOWN` (attempt outcome `INTERRUPTED`). `SUBMISSION_UNKNOWN` blocks every retry until `reconcile_submission` records `ACCEPTED` (→ `SUBMITTED`, receipt with `reconciliation_method`) or definite `NOT_SUBMITTED` (→ `FAILED_RETRYABLE`, which may then be retried as a new attempt).

### Runner recipe (for I1)

```
r = store.record_request(candidate_id, url);  stop unless r.may_proceed
claim = store.claim(r.application.id, owner)
page = await browser.open(url);  store.transition(claim, INSPECTING)
  if page.kind in USER_ACTION_PAGES: await user.request_action(...); page = await browser.wait_for_user(...)
  if page.job_identity: b = store.bind_job_identity(claim, page.job_identity); stop if b.duplicate_of
ctx = PacketContext(app, job, page.form, candidate, store.get_user_inputs(app_id, page.form))
packet = await resolver.resolve(ctx);  reject unless ctx.problems(packet) == []
store.save_packet(claim, packet)
  if not packet.is_complete: store.transition(claim, NEEDS_INPUT)
      inputs = [UserInput.answering(m, value) for each missing field item]   # user chooses reuse
      store.save_user_inputs(claim, inputs); save to_saved_answer(job=) results via SavedAnswerWriter
      resume: re-inspect the current page, then resolve again
store.transition(claim, PACKET_READY); store.transition(claim, FILLING); await browser.fill(form, packet)
  more steps: nav = await browser.advance(); store.transition(claim, INSPECTING); loop
attempt = store.begin_submission(claim, packet_id=packet.id)      # durable BEFORE the click
await browser.submit()
store.record_submission_outcome(claim, attempt.id, await browser.confirm())
report store.get_receipt(app_id) only if state is SUBMITTED
```

Renew the claim (`store.renew`) during long waits such as user sign-in.

## 8. Local data conventions

Personal data never enters source control. `LocalPaths.from_env()` resolves:

| Variable | Default | Contents |
| --- | --- | --- |
| `IMX_HOME` | `~/.interviewmaxxing` | root |
| `IMX_PROFILE_DIR` | `$IMX_HOME/profile` | candidate profile, resume, saved answers (format owned by candidate-brain) |
| `IMX_STATE_DB` | `$IMX_HOME/state/imx.sqlite3` | this store |
| `IMX_ARTIFACTS_DIR` | `$IMX_HOME/artifacts` | evidence, `<application_id>/…` |
| `IMX_BROWSER_DIR` | `$IMX_HOME/browser` | persistent browser profile |
| `IMX_CANDIDATE_ID` | `default` | candidate used by `apply` |

`LocalPaths.ensure()` creates the home, state, artifacts and browser directories with mode `0700`. In a worktree, develop with `IMX_HOME=$PWD/.imx` (ignored). Tests get an isolated temporary `IMX_HOME` automatically (`tests/conftest.py`). Use ephemeral localhost ports for mock servers.

## 9. Fixtures

`tests/fixtures/core/` (fictional candidate "Avery Example"; no real data):

| File | Model |
| --- | --- |
| `candidate_profile.json` | `CandidateProfile` (+ `resume-avery-example.pdf`): verified facts plus one UNVERIFIED (`fact.team_size`, confidence 0.9); GLOBAL saved answers; JOB answers for Mock Co 4012 (`sa.mock_co_start`) and another employer (`sa.other_co_salary`) |
| `application_form.json` | `ApplicationForm` with every operable control type, value≠label options, a disabled placeholder, EEO/consent fields |
| `application_packet.json` | `ApplicationPacket` for that exact form (fingerprint bound); `gender` and `why_us` missing, with question scope |
| `multistep_form_step0.json` / `multistep_form_step1.json` | two steps of one form, each with an unrelated `question_0` |
| `user_input_gender.json` | `UserInput` answering the packet's `gender` item (reuse `APPLICATION`) |
| `job_identity_observation.json` | `JobIdentityObservation` |
| `submission_observation_accepted.json` / `_unknown.json` | `SubmissionObservation` |
| `page_inspection_sign_in.json` | `PageInspection` (`SIGN_IN_REQUIRED`) |

Artifact paths in fixtures are relative to the fixtures directory. Shared pytest fixtures (any package's tests): `core_fixture(name)` (JSON with paths resolved), `fictional_candidate`, `mock_form`, `mock_packet`, `mock_job` (the identity-bound Mock Co `JobRecord` of the packet), `multistep_forms`, `gender_input`, `mock_identity`, `accepted_observation`, `clock` (manually advanced UTC clock), `store_path`, `store`, `isolated_imx_home` (autouse).

## 10. Verification

```bash
scripts/verify.sh          # uv sync --locked --all-packages, ruff, mypy --strict, pytest, CLI smoke
```

## 11. Discovery, Jev selection and pipeline (D0)

Module `interviewmaxxing_core.discovery`, re-exported from `interviewmaxxing_core`. D0 is additive: the application flow, store schema and `ApplicationState` are unchanged, so the contract version stays `2`. Producers: J1 (job-ingestion) writes `JobSearchRun`/`SourceSearchResult`/`JobListing`; J2 (jev-selection) writes `JobSelection`; S1/F3 edit `SelectionPreferences`, `JobSearchQuery` and `PipelineEntry`. Each owning package stores its own records; core defines only the shapes.

**Separation rules**
- `JobListing` is an observed posting and is **not** a `JobRecord`. Selecting a listing does not create an application: the runner calls `record_request(candidate_id, listing.application_url or listing.source_url)` and the existing identity binding, duplicate protection, states and receipts apply unchanged.
- `JobSelection` is an auditable decision. It is not a submission, not a receipt, and not an interview probability (there is no such field).
- `PipelineEntry.stage` is the user's own wording (for example imported workbook status text), kept verbatim. It is not an `ApplicationState`: moving a card never submits, and a submission never rewrites the stage. `application_id` optionally links the canonical application. `PipelineStages` (default `DEFAULT_PIPELINE_STAGES`: Interested, Applied, Screening, Interviewing, Offer, Closed) is the user-configurable column list; entries may carry stages outside it.

**Search (`JobSearchQuery`)**
- Fields: `id`, `title_phrases`, `keywords`, `excluded_keywords`, `onsite: list[OnsiteTarget(location, arrangements⊆{ONSITE,HYBRID}, radius_miles)]`, `remote: RemoteTarget(eligible_region) | None`, `minimum_compensation: CompensationFloor(amount, currency, period) | None`, `sources`, `max_results_per_source` (1–500, default 50), `posted_within_days` and `created_at`.
- Defaults are the user's targets: `["marketing manager", "marketing director"]`, onsite/hybrid in `"Austin, TX"`, remote eligible in `"United States"`, and a floor of 100000 USD per YEAR. Sources default to `KNOWN_SOURCES` = linkedin, builtin, indeed, google; any lower-case slug is allowed. `JobSearchQuery.from_preferences(prefs, **overrides)` builds a query from edited preferences.
- **Remote eligibility is separate from the onsite city.** US-wide remote is `RemoteTarget(eligible_region="United States")`; a remote-in-Texas search is not the requested nationwide search.
- A floor is for ranking and filtering only. A listing without comparable pay is kept, with pay unknown.

**Per-source outcome (`SourceSearchResult`)**
- `state`:
  - `OK` — searched; zero results is a real result.
  - `PARTIAL` — some listings were collected, then the search stopped.
  - `NEEDS_USER` — requires `message`, `user_action` and preferably `session_name` (e.g. `imx-jobs-linkedin`).
  - `BLOCKED`, `ERROR` and `SKIPPED` — each requires `message`.
- `BLOCKED`, `NEEDS_USER` and `SKIPPED` cannot carry listings, so a blocked source is never a silent empty success. `result_count` is `len(listing_ids)`.
- `JobSearchRun(id, query, results, started_at, finished_at)` checks that every result belongs to its query and to one of its sources.

**Listings (`JobListing`)**
- Fields: `id`, `source`, `source_listing_id`, `posting_url`, `source_url`, `application_url`, `title`, `company`, `location`, `work_arrangement` (`ONSITE|HYBRID|REMOTE|UNKNOWN`), `remote_eligibility`, `compensation`, `description`, `description_completeness` (`FULL|PARTIAL|NONE`), `status` (`OPEN|CLOSED|UNKNOWN`), `posted_text`, `observed_at`, `evidence` and `provenance: list[ListingSource]` (at least one; `provenance[0]` repeats the listing's own `source`, `source_listing_id`, `posting_url` and `source_url`).
- Unshown data stays `None`/`UNKNOWN`. `description_completeness` is `NONE` exactly when there is no text.
- `Compensation(raw_text, minimum, maximum, currency, period)`: `raw_text` is verbatim. Numeric bounds are allowed only with an explicit currency and period, and are never estimated.
- `meets_floor(pay, floor) -> bool | None` is deterministic:
  - `True` when the stated range reaches the floor.
  - `False` when its top is below the floor.
  - `None` (unknown) for missing pay, another currency, or periods not exactly convertible. Only MONTH↔YEAR ×12 is converted.

**Posting identity and dedupe (D0R).** Where a posting was seen is separate from what it is. Each `ListingSource` observation has:

| Field | Meaning | Identity? |
| --- | --- | --- |
| `source_url` | Where it was observed; may be a search or results page shared by many postings | never |
| `source_listing_id` | The source's own job id (LinkedIn job id, Indeed `jk`, …) | yes, within the source |
| `posting_url` | A URL showing **this posting alone** (a detail page or job-specific ATS posting) | yes, within the source, when there is no id |
| `employer_job_key` | Proven cross-source employer job, `employer_job_key(ats_type, tenant, job_id)` → `ats:<type>:<tenant>:<job id>` (the `JobIdentityObservation.identity_key` format) | yes, across sources |
| `application_url` | The apply link shown; may be a generic endpoint shared by many jobs | never |

- **J1 construction rules**
  1. Every observation needs `source_listing_id` or a job-specific `posting_url`. An observation with neither is rejected (`ValueError`/`ValidationError`), so a search URL can never become an identity. If a result has no job-specific link, open it or leave it out; do not key it on the results page.
  2. Set `id = listing_id_for(source, source_listing_id, posting_url)`. The listing validates this, and ids use the same key as `ListingSource.posting_key` (`src:<source>:id:<id>`, else `src:<source>:url:<normalized posting URL>`). Equal ids therefore always mean the same posting.
  3. Set `employer_job_key` only from job-specific evidence: an ATS posting URL or page containing this job's own id, such as `boards.greenhouse.io/<tenant>/jobs/<id>` or `jobs.lever.co/<tenant>/<id>`. Say where the id was read in `evidence`. Never set it from a careers home page, a generic apply endpoint, a search page or a title/company match.
  4. Put Google (and other aggregator) outbound results under their own `source` with the job-specific target as `posting_url`. They merge with another source's listing only through a shared `employer_job_key` (or, rarely, the same source's id or posting URL).
- **Rules the contract enforces**
  - `identity_keys` holds each observation's `posting_key` plus `job:<employer_job_key>`.
  - `contradicts(other)` is true when one source gives the two listings different ids of its own, or they carry different employer keys. A single listing with such contradictions is rejected.
  - `is_same_posting(other)` requires a shared key and no contradiction. The same title/company/location, a shared search page or a shared application URL never counts.
  - `merged_with(other)` keeps this listing's values and appends the other's provenance. It fills only missing fields, prefers the fuller description, and any `CLOSED` wins. It raises unless the two are provably the same posting, so a closed posting can never close a different one.

**Preferences and decisions**
- `SelectionPreferences`:
  - Fields: `target_titles`, `onsite`, `remote`, `minimum_compensation`, `unknown_compensation` (`KEEP` default | `REVIEW`), `excluded_keywords`, `excluded_companies` and `notes`. Defaults are the same as the query's.
  - `.fingerprint` is the canonical SHA-256 and changes with any edit.
- `JobSelection` fields:
  - Identity and inputs: `id`, `listing_id`, `candidate_id`, `requested_model` (default `DEFAULT_JEV_MODEL` = `typesafe/jev-1.13`), `returned_model`, `rubric_version`, `preferences_fingerprint`, `job_evidence_hash` and `candidate_evidence_hash` (`None` = no usable profile).
  - Outcome: `model_decision: ModelDecision(choice, probabilities over exactly APPLY/SKIP/REVIEW, finite and summing to 1 ±0.001, confidence 0..1, provider, decision_id)`, `provider_error: ProviderError(code, message, retryable)`, `usage: ProviderUsage(tokens, cost_usd)`, `holds: list[PolicyHold(code, detail)]`, `override: SelectionOverride(choice, reason, decided_at)`, `effective_choice`, `reasons` and `decided_at`.
  - `HoldCode`: `MISSING_PROFILE`, `INSUFFICIENT_EVIDENCE`, `PROVIDER_ERROR`, `LOW_CONFIDENCE`, `HARD_CONSTRAINT`, `UNKNOWN_COMPENSATION`, `LISTING_CLOSED`, `DUPLICATE_APPLICATION`.
- Enforced by validation:
  - A decision cannot have both a model decision and a provider error.
  - A provider error needs a `PROVIDER_ERROR` hold, and missing candidate evidence needs a `MISSING_PROFILE` hold.
  - A model decision needs `returned_model`.
  - An override fixes `effective_choice`.
  - **Effective APPLY is impossible** with any hold, a provider error, no model decision, or no candidate evidence. Without an explicit user override it also requires Jev's own APPLY.
  - Jev's original answer is never rewritten; holds and overrides sit beside it.
- `snapshot_hash(model_or_mapping)` is the canonical SHA-256 for evidence hashes. `selection.is_current_for(preferences=, job_evidence_hash=, candidate_evidence_hash=, rubric_version=)` is the cache check; any preference edit makes older decisions stale.

**Examples** (fictional; validated by `tests/core/test_discovery.py`, which parses every block below):

<!-- D0-EXAMPLE: JobSearchQuery -->
```json
{
  "id": "qry_example",
  "title_phrases": [
    "marketing manager",
    "marketing director"
  ],
  "keywords": [],
  "excluded_keywords": [],
  "onsite": [
    {
      "location": "Austin, TX",
      "arrangements": [
        "ONSITE",
        "HYBRID"
      ],
      "radius_miles": null
    }
  ],
  "remote": {
    "eligible_region": "United States"
  },
  "minimum_compensation": {
    "amount": 100000.0,
    "currency": "USD",
    "period": "YEAR"
  },
  "sources": [
    "linkedin",
    "builtin",
    "indeed",
    "google"
  ],
  "max_results_per_source": 50,
  "posted_within_days": null,
  "created_at": "2026-09-22T21:00:00Z",
  "location_priority": "STRONGLY_PREFER_ONSITE_HYBRID"
}
```

<!-- D0-EXAMPLE: SourceSearchResult -->
```json
{
  "query_id": "qry_example",
  "source": "linkedin",
  "state": "NEEDS_USER",
  "listing_ids": [],
  "pages_visited": 0,
  "message": "LinkedIn shows a sign-in wall before search results.",
  "user_action": "Sign in to LinkedIn in the imx-jobs-linkedin browser session, then retry.",
  "session_name": "imx-jobs-linkedin",
  "started_at": "2026-09-22T21:00:00Z",
  "finished_at": "2026-09-22T21:00:00Z"
}
```

<!-- D0-EXAMPLE: JobListing -->
```json
{
  "id": "lst_542448c2ff017a0c096a518a67984655",
  "source": "linkedin",
  "source_listing_id": "4001",
  "posting_url": "https://www.linkedin.example/jobs/view/4001",
  "source_url": "https://www.linkedin.example/jobs/search?keywords=marketing+manager&location=Austin",
  "application_url": "https://boards.greenhouse.example/fictionalco/jobs/7001",
  "title": "Senior Marketing Manager",
  "company": "Fictional Co",
  "location": "Austin, TX",
  "work_arrangement": "HYBRID",
  "remote_eligibility": null,
  "compensation": {
    "raw_text": "$110,000 - $130,000/yr",
    "minimum": 110000.0,
    "maximum": 130000.0,
    "currency": "USD",
    "period": "YEAR"
  },
  "description": "Lead lifecycle and paid media programs for a fictional B2B product.",
  "description_completeness": "PARTIAL",
  "status": "OPEN",
  "posted_text": "3 days ago",
  "observed_at": "2026-09-22T21:00:00Z",
  "evidence": "search card and detail pane; job id 4001 in URL",
  "provenance": [
    {
      "source": "linkedin",
      "source_listing_id": "4001",
      "posting_url": "https://www.linkedin.example/jobs/view/4001",
      "source_url": "https://www.linkedin.example/jobs/search?keywords=marketing+manager&location=Austin",
      "employer_job_key": "ats:greenhouse:fictionalco:7001",
      "application_url": "https://boards.greenhouse.example/fictionalco/jobs/7001",
      "observed_at": "2026-09-22T21:00:00Z",
      "evidence": "search card; job id 4001 in URL; Apply links to Greenhouse job 7001",
      "query_id": "qry_example"
    },
    {
      "source": "google",
      "source_listing_id": null,
      "posting_url": "https://boards.greenhouse.example/fictionalco/jobs/7001",
      "source_url": "https://www.google.example/search?q=fictional+co+marketing+manager",
      "employer_job_key": "ats:greenhouse:fictionalco:7001",
      "application_url": "https://boards.greenhouse.example/fictionalco/jobs/7001?gh_src=google",
      "observed_at": "2026-09-22T21:00:00Z",
      "evidence": "Google Jobs card linking Greenhouse job 7001 (id in the posting URL)",
      "query_id": "qry_example"
    }
  ]
}
```

<!-- D0-EXAMPLE: SelectionPreferences -->
```json
{
  "target_titles": [
    "marketing manager",
    "marketing director"
  ],
  "onsite": [
    {
      "location": "Austin, TX",
      "arrangements": [
        "ONSITE",
        "HYBRID"
      ],
      "radius_miles": null
    }
  ],
  "remote": {
    "eligible_region": "United States"
  },
  "minimum_compensation": {
    "amount": 100000.0,
    "currency": "USD",
    "period": "YEAR"
  },
  "unknown_compensation": "KEEP",
  "excluded_keywords": [],
  "excluded_companies": [],
  "notes": null,
  "location_priority": "STRONGLY_PREFER_ONSITE_HYBRID"
}
```

<!-- D0-EXAMPLE: JobSelection -->
```json
{
  "id": "sel_example",
  "listing_id": "lst_542448c2ff017a0c096a518a67984655",
  "candidate_id": "default",
  "requested_model": "typesafe/jev-1.13",
  "returned_model": "typesafe/jev-1.13-20260917",
  "rubric_version": "selection-rubric-1",
  "preferences_fingerprint": "a9621ce5ff116ebe9ff6d0e2d054eeb5cf8846e49d6b38913b61c013aa5df9a2",
  "job_evidence_hash": "4504aedafa9dcac891a2f53c3a915d6d3f0384be3598a1c9470e642e041a32cc",
  "candidate_evidence_hash": "c2b4b28561c7b311142d7314892152036ea7c609d4485c2f8d48969d44d8cdff",
  "model_decision": {
    "choice": "APPLY",
    "probabilities": {
      "APPLY": 0.72,
      "SKIP": 0.08,
      "REVIEW": 0.2
    },
    "confidence": 0.72,
    "provider": "TypeSafe",
    "decision_id": "dec_fictional"
  },
  "provider_error": null,
  "usage": {
    "prompt_tokens": 850,
    "completion_tokens": 40,
    "total_tokens": 890,
    "cost_usd": 0.0031
  },
  "holds": [],
  "override": null,
  "effective_choice": "APPLY",
  "reasons": [
    "Title matches 'marketing manager'",
    "Hybrid in Austin, TX",
    "Stated pay $110k-$130k meets the $100k floor"
  ],
  "decided_at": "2026-09-22T21:00:00Z"
}
```

<!-- D0-EXAMPLE: PipelineEntry -->
```json
{
  "id": "pipe_example",
  "candidate_id": "default",
  "listing_id": "lst_31a1b821d73a8a89295a841ff2093919",
  "title": "Senior Marketing Manager",
  "company": "Fictional Co",
  "stage": "Waiting on recruiter",
  "notes": "Referral from a fictional contact.",
  "next_action": "Follow up by email",
  "next_action_due": "2026-09-29T16:00:00Z",
  "application_id": null,
  "selection_id": "sel_example",
  "import_source": null,
  "imported_values": {},
  "created_at": "2026-09-22T21:00:00Z",
  "updated_at": "2026-09-22T21:00:00Z"
}
```

## Change requests

Send the coordinator: the contract/type, the exact field or signature change, why the current contract cannot express it, and which tests/fixtures demonstrate it. Additive optional fields are cheap; renames, removed fields, and state-machine changes require coordinator approval and a `CONTRACT_VERSION` bump.

### 2026-09-22 preference and provenance correction

`JobSearchQuery.location_priority` and `SelectionPreferences.location_priority` use `LocationPriority`: `STRONGLY_PREFER_ONSITE_HYBRID` (default), `BALANCED`, or `PREFER_REMOTE`. The user strongly prefers Austin onsite/hybrid over US-wide remote. Remote remains eligible; USD100000 annual minimum is unchanged. Preserve this field through the frontend/service boundary, include it in Jev evidence/rubric and cache fingerprints, and order eligible matching Austin onsite/hybrid results well above remote. Do not infer Austin eligibility from a missing location or confuse this preference with a mandatory remote exclusion.

Repeated observations of the same source posting retain newly verified employer identity and its evidence. `PipelineEntry.next_action_due` accepts a date or an aware timestamp; date-only input must stay a date. `imported_values` includes all original nonblank cells, especially raw Stage and Status; the board column does not overwrite their imported wording.
