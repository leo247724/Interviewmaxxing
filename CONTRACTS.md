# Interviewmaxxing contracts

Owner: **core-contracts** (WT-00). Contract version `1` (`interviewmaxxing_core.CONTRACT_VERSION`).
Scope: the supplied-URL MVP (ARCHITECTURE.md §2 and §17). Discovery, Jev selection, queues and dashboards are out of scope.

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

Then `uv sync --all-packages`. The glob membership needs no root edit. `uv.lock` stays core-owned: adding a member or dependency regenerates it, and you may commit that regenerated lockfile on your branch so `scripts/verify.sh` (which uses `--locked`) passes, but list every new third-party dependency in your handoff. At merge the coordinator re-runs `uv lock` instead of merging lockfile text. Do not edit the root `pyproject.toml`, `tests/conftest.py`, or `scripts/verify.sh`; request changes instead. `scripts/verify.sh` lints and type-checks (`mypy --strict`) every workspace member's sources.

## 2. Core module map

Everything below is re-exported from `interviewmaxxing_core`.

| Module | Contents |
| --- | --- |
| `forms` | `SemanticType`, `ControlType`, `FieldOption`, `ApplicationField`, `ApplicationForm`, `EXPLICIT_ANSWER_REQUIRED`, `PROTECTED_ATTRIBUTE_TYPES` |
| `candidate` | `CandidateProfile`, `CandidateIdentity`, `PostalAddress`, `CandidateFact`, `ResumeArtifact`, `SavedAnswer`, `Experience`, `Education` |
| `artifacts` | `ArtifactRef`, `EvidenceRef`, `EvidenceKind`, `sha256_file` |
| `jobs` | `JobIdentityObservation`, `IdentityEvidenceKind`, `JobRecord` |
| `packets` | `ApplicationPacket`, `PacketAnswer`, `Provenance`, `AnswerSource`, `AnswerValue` (`TextValue`/`ChoiceValue`/`MultiChoiceValue`/`BooleanValue`/`FileValue`), `MissingInput`, `MissingReason`, `UserInput`, `answer_problems` |
| `execution` | `PageInspection`, `PageKind`, `FillResult`, `FieldFillResult`, `FieldFillStatus`, `NavigationResult`, `SubmitActionResult`, `SubmissionObservation`, `SubmissionOutcome`, `NotSubmittedNext`, `SubmissionReconciliation`, `ReconciliationMethod` |
| `applications` | `ApplicationState`, `TRANSITIONS`, state sets, `ApplicationRequest`, `Application`, `ApplicationEvent`, `Claim`, `SubmissionAttempt`, `Receipt`, `RequestDisposition` |
| `interfaces` | service `Protocol`s (§6) and `PacketContext`, `BrowserOptions`, `ApplyOutcome` |
| `store` | `ApplicationStore`, `RequestResult`, `BindResult` |
| `errors` | `StoreError`, `NotFound`, `InvalidTransition`, `ClaimUnavailable`, `ClaimLost`, `SubmissionBlocked`, `IdentityConflict` |
| `urls` | `normalize_application_url`, `InvalidApplicationUrl` |
| `config` | `LocalPaths` |

All contracts derive from `Contract`: Pydantic v2, **frozen** (use `model_copy(update=...)`), **extra fields forbidden**, datetimes **timezone-aware and normalized to UTC** (naive datetimes are rejected). Every contract serializes with `model_dump_json()` / `model_validate_json()` and publishes `model_json_schema()`.

## 3. Candidate data (candidate-brain produces)

`CandidateProfile(id, identity, resume, facts, saved_answers, experience, education)`

- `CandidateIdentity` — verified contact details; `verified_at` is **required**.
- `CandidateFact(id, key, value, source, confidence, evidence)` — factual claims with source and supporting quotes. Every generated claim must trace to fact ids.
- `ResumeArtifact(ArtifactRef)` — the supplied resume: absolute local `path`, `sha256`, `size_bytes`, `media_type`, `variant="supplied"`, optional `extracted_text`. `verify()` rechecks the file digest.
- `SavedAnswer(id, semantic_type, question, match_phrases, value, confirmed_at)` — answers the user explicitly gave for reuse; `value` is in the user's terms (`"Yes"`, `False`, list of labels). Mapping to a form's option values is the resolver's job, and ambiguity must be reported, not guessed.
- Experience/education `fact_ids` must reference existing facts (validated).

## 4. Forms, packets and answers

### Fields

`ApplicationField(id, label, semantic_type, control_type, selector, required, input_type, options, accept, max_length, placeholder, help_text, validation_error)`
`ApplicationForm(url, ats_type, step, fields, is_final_step, submit_selector, next_selector, page_errors, inspected_at)`

- `id` is stable within the form and is the answer key. Packets never address selectors.
- `FieldOption(value, label, selector, disabled)`: **`value` is the machine value** the page submits and the one a packet selects; **`label` is the user-visible text**. Choice controls require options with unique values; non-choice controls must not have options; `accept` is FILE-only.
- `SemanticType` follows ARCHITECTURE.md §7, plus `FULL_NAME`, `PREFERRED_NAME`, `COUNTRY`, `LOCATION`, `CURRENT_COMPANY`, `CURRENT_TITLE`, `START_DATE`, `RELOCATION`, `REFERRAL_SOURCE`, `EEO_*`, `PRONOUNS`, `CONSENT`, `ATTESTATION`, `CUSTOM_TEXT`, `UNKNOWN`.

| `ControlType` | Answer value | Notes |
| --- | --- | --- |
| `TEXT` | `TextValue(text)` | `input_type` = HTML type (email, tel, url, number, date) |
| `TEXTAREA` | `TextValue(text)` | `max_length` enforced |
| `SELECT` | `ChoiceValue(value, label)` | single choice |
| `RADIO` | `ChoiceValue(value, label)` | per-option `selector` |
| `MULTISELECT` | `MultiChoiceValue(choices: list[FieldOption])` | |
| `CHECKBOX_GROUP` | `MultiChoiceValue(choices)` | several checkboxes, one question |
| `CHECKBOX` | `BooleanValue(checked)` | a required checkbox must be checked |
| `FILE` | `FileValue(artifact: ArtifactRef)` | |
| `UNSUPPORTED` | none | report as `MissingInput(reason=UNSUPPORTED_CONTROL)` |

`answer_problems(field, value) -> list[str]` checks kind/option/length/required compatibility; `ApplicationPacket.problems_against(form)` checks a whole packet (unknown fields, bad values, and required fields neither answered nor reported missing).

### Packets (application-packets produces)

`ApplicationPacket(id, application_id, job_id, candidate_id, form_url, form_step, resume_variant, answers, missing_inputs, cover_letter, created_at)`; properties `unresolved_fields`, `is_complete`; `answer_for(field_id)`.

`PacketAnswer(field_id, semantic_type, value, provenance, confidence)` with `Provenance(source, reference_ids, note)`:

| `AnswerSource` | `reference_ids` |
| --- | --- |
| `PROFILE_IDENTITY` | optional |
| `CANDIDATE_FACT`, `GENERATED_FROM_FACTS` | the fact ids (required) |
| `SAVED_ANSWER` | saved-answer ids (required) |
| `RESUME` | the artifact id (required) |
| `USER_INPUT` | `UserInput.id` (required) |

**Never infer.** For `EXPLICIT_ANSWER_REQUIRED` — work authorization, sponsorship, salary, consent, attestation, EEO/protected attributes and pronouns — a `PacketAnswer` is *rejected at construction* unless its source is `SAVED_ANSWER` or `USER_INPUT`. Without one, emit `MissingInput`.

`MissingInput(id, field_id, label, reason, prompt, semantic_type, control_type, options, required, candidates)`; `reason` ∈ `NO_ANSWER`, `EXPLICIT_ANSWER_REQUIRED`, `UNCOVERED_ATTESTATION`, `AMBIGUOUS`, `UNSUPPORTED_CONTROL`, `USER_ACTION` (sign-in/CAPTCHA; the only reason that may omit `field_id`).

`UserInput(id, field_id, question, semantic_type, value, save_for_reuse, provided_at)` — the user's answer, persisted by the store for resume.

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
    # raises CandidateNotFound / CandidateProfileInvalid; never fabricates data

@dataclass(frozen=True)
class PacketContext:
    application: Application
    job: JobRecord
    form: ApplicationForm
    candidate: CandidateProfile
    user_inputs: Sequence[UserInput] = ()

class PacketResolver(Protocol):                                # application-packets
    async def resolve(self, context: PacketContext) -> ApplicationPacket: ...
    # result must satisfy packet.problems_against(context.form) == []

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
save_user_inputs(claim, inputs);  get_user_inputs(app_id)        # latest per field
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
- **Crash / uncertainty**: if the owner of a `SUBMITTING` application disappears (claim lapses or is released), the next `claim()` or `recover_interrupted_submissions()` marks it `SUBMISSION_UNKNOWN` (attempt outcome `INTERRUPTED`). `SUBMISSION_UNKNOWN` blocks every retry until `reconcile_submission` records `ACCEPTED` (→ `SUBMITTED`, receipt with `reconciliation_method`) or definite `NOT_SUBMITTED` (→ `FAILED_RETRYABLE`, which may then be retried as a new attempt).

### Runner recipe (for I1)

```
r = store.record_request(candidate_id, url);  stop unless r.may_proceed
claim = store.claim(r.application.id, owner)
page = await browser.open(url);  store.transition(claim, INSPECTING)
  if page.kind in USER_ACTION_PAGES: await user.request_action(...); page = await browser.wait_for_user(...)
  if page.job_identity: b = store.bind_job_identity(claim, page.job_identity); stop if b.duplicate_of
packet = await resolver.resolve(PacketContext(..., user_inputs=store.get_user_inputs(app_id)))
store.save_packet(claim, packet)
  if not packet.is_complete: store.transition(claim, NEEDS_INPUT); ask; store.save_user_inputs(...); re-inspect
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

`LocalPaths.ensure()` creates the state, artifacts and browser directories with mode `0700`. In a worktree, develop with `IMX_HOME=$PWD/.imx` (ignored). Tests get an isolated temporary `IMX_HOME` automatically (`tests/conftest.py`). Use ephemeral localhost ports for mock servers.

## 9. Fixtures

`tests/fixtures/core/` (fictional candidate "Avery Example"; no real data):

| File | Model |
| --- | --- |
| `candidate_profile.json` | `CandidateProfile` (+ `resume-avery-example.pdf`) |
| `application_form.json` | `ApplicationForm` with every operable control type, value≠label options, EEO/consent fields |
| `application_packet.json` | `ApplicationPacket` answering that form; `gender` and `why_us` missing |
| `job_identity_observation.json` | `JobIdentityObservation` |
| `submission_observation_accepted.json` / `_unknown.json` | `SubmissionObservation` |
| `page_inspection_sign_in.json` | `PageInspection` (`SIGN_IN_REQUIRED`) |

Artifact paths in fixtures are relative to the fixtures directory. Shared pytest fixtures (any package's tests): `core_fixture(name)` (JSON with paths resolved), `fictional_candidate`, `mock_form`, `mock_packet`, `mock_identity`, `accepted_observation`, `clock` (manually advanced UTC clock), `store_path`, `store`, `isolated_imx_home` (autouse).

## 10. Verification

```bash
scripts/verify.sh          # uv sync --locked --all-packages, ruff, mypy --strict, pytest, CLI smoke
```

## Change requests

Send the coordinator: the contract/type, the exact field or signature change, why the current contract cannot express it, and which tests/fixtures demonstrate it. Additive optional fields are cheap; renames, removed fields, and state-machine changes require coordinator approval and a `CONTRACT_VERSION` bump.
