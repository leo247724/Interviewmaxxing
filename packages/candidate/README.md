# interviewmaxxing-candidate

Loads the user's verified candidate profile, supplied resume and saved answers from local storage. It also persists answers the user chose to reuse. It implements the core `CandidateLoader` and `SavedAnswerWriter` interfaces (CONTRACTS.md §3 and §6) and returns core contract types only.

## Construction (for I1 and the frontend bridge)

```python
from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_core import LocalPaths

store = LocalCandidateStore.from_paths(LocalPaths.from_env())  # profile_dir = IMX_PROFILE_DIR
store = LocalCandidateStore.from_env(env=None)                 # same, from os.environ or a mapping
store = LocalCandidateStore(profile_dir, *, max_resume_bytes=10 * 1024 * 1024, clock=utc_now)
    # profile_dir: Path | str; relative → resolved against CWD now. from_paths takes the same keywords.

profile: CandidateProfile = store.load(candidate_id)           # CandidateLoader
report: CandidateLoadReport = store.load_report(candidate_id)  # load() plus sources, superseded answers and conflicts
store.save_answer(candidate_id, saved_answer)                  # SavedAnswerWriter
store.exists(candidate_id) -> bool
store.candidate_dir(id) / store.profile_path(id) / store.answers_path(id) / store.resumes_dir(id) -> Path
```

The store holds no open resources and is safe to construct per request. The runner's missing-input step is:

```python
saved = user_input.to_saved_answer(job=job)  # None for AnswerReuse.APPLICATION
if saved is not None:
    store.save_answer(candidate_id, saved)
```

## Candidate setup (for S1: `GET /candidate`, `POST /resumes`, starting an application)

`candidate_id` is always the configured stable id (`LocalPaths.candidate_id`, `IMX_CANDIDATE_ID`, default `default`), never derived from content or generated per request.

```python
store.store_resume(candidate_id, *, filename: str, content: bytes,
                   media_type: str | None = None) -> StoredResume
    # Raises ResumeRejected (400-type input error; nothing stored) or CandidateNotFound (invalid id).
store.list_resumes(candidate_id) -> list[StoredResume]
store.get_resume(candidate_id, resume_id: str) -> StoredResume          # raises ResumeNotFound
store.upsert_profile(candidate_id, *, identity: CandidateIdentity,
                     resume_id: str) -> CandidateProfile
    # Raises ResumeNotFound, CandidateProfileInvalid (nothing written), CandidateNotFound (invalid id).
store.candidate_setup(candidate_id) -> CandidateSetup                  # never raises CandidateNotFound for a valid id

@dataclass(frozen=True)
class StoredResume:
    artifact: ResumeArtifact        # canonical: id, absolute path, filename, media_type, sha256, size_bytes
    origin: ResumeOrigin            # UPLOADED | PROFILE
    uploaded_at: datetime | None    # UTC; None for a PROFILE resume (it was not uploaded here)
    id: str                         # property, = artifact.id

@dataclass(frozen=True)
class CandidateSetup:
    candidate_id: str
    identity: CandidateIdentity | None   # None until contact details are saved
    resumes: tuple[StoredResume, ...]    # = list_resumes()
    selected_resume_id: str | None       # the resume profile.json references
    complete: bool                       # load() succeeds: ready to apply
    problem: str | None                  # CandidateProfileInvalid message (contains local paths) when not loadable
```

Mapping to the frontend `CandidateView`: `profile` ← `identity` (all fields empty when `None`); `resumes[]` ← `{id, fileName: artifact.filename, sizeBytes: artifact.size_bytes, uploadedAt: uploaded_at}` (a PROFILE resume has no upload time); `defaultResumeId` ← `selected_resume_id`. `CandidateProfileInput.location` is one string while `CandidateIdentity.address` is structured. The service decides how to map it; this package does not parse it.

- **Uploads before a profile exists.** `store_resume` needs only a valid candidate id. Nothing about contact details or facts is created, and the upload is not selected until `upsert_profile` names it.
- **Upload rules.** The filename is reduced to its last path component, ASCII `[A-Za-z0-9._()- ]` characters, no leading dots and at most 100 characters before the extension. The extension must be one of `RESUME_UPLOAD_TYPES` (`.pdf .doc .docx .odt .rtf .txt .md`). `media_type` may be omitted or `application/octet-stream`; any other value must equal the extension's type. The content must be non-empty and at most `max_resume_bytes` (default 10 MiB). It must also look like its type: `%PDF-`, the OLE or ZIP signature, `{\rtf`, or UTF-8 text for `.txt`/`.md`. The bytes are stored unchanged under a generated id `resume_<32 hex>`. Uploads are never modified, and their content is never read for facts or qualifications.
- **Storage.** Each upload is `<candidate_id>/resumes/<resume_id>/<safe filename>` plus `meta.json`. Both files are `0400` and the directories `0700`. The upload directory is assembled under a hidden staging name and renamed into place, so it is complete or absent. Reads recheck the recorded sha256.
- **`upsert_profile`.** It stores `identity` exactly as given. Pass `verified_at` as the time the user confirmed those details on screen. It selects `resume_id`, which must come from `list_resumes()`:
  - An upload is referenced from `profile.json` as `resumes/<id>/<file>` with its digest.
  - Choosing the profile's current resume leaves that entry untouched. This includes an imported or hand-written profile whose resume lives elsewhere. Request-supplied paths are never accepted or copied.
  - Every other key of an existing `profile.json` is kept verbatim: facts, experience, education, and embedded `saved_answers`, including tied conflicts. Loading reconciles them but this function does not. `answers.json` and application history (the state database) are untouched, and the candidate id does not change.
  - A new profile starts with empty facts, answers, experience and education.
  - The resulting profile is validated as `load()` would before it is written, atomically (`0600`), under the same per-candidate lock as `save_answer`. An existing profile that cannot be parsed or validated raises `CandidateProfileInvalid`, and nothing is overwritten.
- **Concurrency.** Uploads need no lock because each has a fresh id and an atomic rename. `upsert_profile` and `save_answer` serialize on `<candidate_id>/.answers.lock`.

## Files

Under `LocalPaths.profile_dir` (`$IMX_HOME/profile`; outside source control):

```
<candidate_id>/
    profile.json   CandidateProfile JSON, written by the user or by upsert_profile
    answers.json   JSON array of SavedAnswer, written by save_answer (may also be edited by hand)
    resumes/       uploads from store_resume, one immutable directory per resume id
    resume.pdf     optional location for a hand-placed resume; resume.path may point anywhere
```

- **Candidate id.** The directory name is the candidate id. It is never derived from content, so editing the profile keeps it. `profile.json`'s `"id"` must equal it. Ids are 1-128 characters of `[A-Za-z0-9._-]` starting with a letter or digit, and other ids raise `CandidateNotFound`. The CLI default id is `default` (`IMX_CANDIDATE_ID`).
- **`profile.json`** uses the exact canonical `CandidateProfile` schema. Unknown fields, naive datetimes, facts without `verification`, and facts without `value` are rejected. `null`, `false`, `0`, `""` and `[]` are kept as given. For the resume only, the file-derived fields `sha256`, `size_bytes`, `filename` and `media_type` may be omitted. The loader computes them from the file, guessing the media type from the extension (see `RESUME_MEDIA_TYPES`). A given `sha256` or `size_bytes` must match the file.
- **`resume.path`** may be absolute, `~/…`, or relative. A relative path is resolved against the directory containing `profile.json`, never the working directory. The file must exist, be a regular file and be non-empty. The returned `ResumeArtifact.path` is absolute. `extracted_text` is passed through as supplied; the loader never extracts text or qualifications from the resume.
- **`answers.json`** is an array of canonical `SavedAnswer` objects. A missing file means no stored answers. Ids must be unique across both files.

`examples/candidate.example.json` and `examples/answers.example.json` are a fictional starting point. Copy them to `<profile_dir>/default/profile.json` and `answers.json`, then put the resume beside them as `resume.pdf`.

## What loading guarantees

- **Nothing is fabricated or upgraded.** The loader never sets `verified_at`, never changes a fact's verification and never changes an answer's scope. Unverified facts are returned unchanged. Only `verified_facts()` / `verified_only()` should feed answers. `report.unverified_fact_ids` and `report.warnings()` let the UI ask the user to confirm them.
- **Scope is explicit.** A `JOB` answer needs `job_identity_key` or `job_url`, and a `GLOBAL` answer may not name a job or employer. Otherwise the whole load fails. Matching remains `SavedAnswer.applies_to(job)`, so a job-specific answer never applies to another job or employer.
- **Saved attestations work as given.** An explicitly saved attestation or consent answer loads like any other answer; no per-application approval is added.
- **Same question, different answers.** Two answers ask the same question when their scope, `job_identity_key`, `job_url`, `semantic_type` and `normalize_text(question)` are all equal. Values compare type-strictly: `False` ≠ `0` ≠ `"No"`. Text compares with `normalize_text`, and label lists compare as sets. When answers to one question disagree, only the newest confirmation (latest `confirmed_at`) counts:
  - **Superseded.** An older answer whose value differs from every newest answer is left out of the profile and listed in `report.superseded_answers`, with `superseded_by` naming the newest answer ids.
  - **Conflict.** If the newest answers disagree with each other (the same `confirmed_at`, different values), all of them **stay in `profile.saved_answers`** and are listed in `report.answer_conflicts`. The resolver sees both and must report the field as `AMBIGUOUS` instead of choosing one. Dropping them would let a less specific answer fill the field silently. For example, with a GLOBAL salary of 100000 and two JOB salaries (150000, 175000) confirmed at the same time for one job, the job's salary question is ambiguous; it is not answered with 100000.
  - **Kept.** Everything else is returned unchanged, including older answers whose value matches a newest one.

  Answers with different scopes or targets are never merged. For example, when a JOB answer and a GLOBAL answer both apply to a job, both are returned and the resolver decides.
- **Errors are actionable.** `CandidateNotFound` names the expected `profile.json` path. `CandidateProfileInvalid` names the file and either the JSON line and column or each schema problem as a location such as `facts[0].verification: Field required` or `[2].scope: Field required` in `answers.json`. Resume problems give the resolved path and what to change. JSON with duplicate keys, `NaN` or `Infinity` is rejected.

## Writing answers

`save_answer(candidate_id, answer)` writes the `SavedAnswer` exactly as given, scope included, to `answers.json`:

- It writes atomically and with owner-only permissions (`0600`). A lock file serializes concurrent writers (the CLI and the frontend).
- It compares the new answer with stored answers to the same question (same key as above) by `confirmed_at`, under the lock:
  - Older stored answers are replaced.
  - If a stored answer is newer, the new one is stale: nothing is written, so a delayed retry of an old answer cannot overwrite a newer one.
  - A stored answer with the same `confirmed_at` and the same value makes the save a retry, so nothing is written. This covers a retried `to_saved_answer`, which gets a fresh id. `False` and `0` are different values here.
  - A stored answer with the same `confirmed_at` and a different value is kept alongside the new one; the writer does not pick whichever came last. Loading then reports the pair as a conflict (above).
- Answers with another scope or target are untouched, so saving a JOB answer can never replace or widen a GLOBAL one, or the reverse. Answers in `profile.json` are not compared at write time; loading reconciles them by `confirmed_at` as above.
- Saving an identical answer again is a no-op. Reusing an id for a different answer, or an id already present in `profile.json`, raises `SavedAnswerRejected` and writes nothing.
- It requires an existing `profile.json` (`CandidateNotFound` otherwise). A corrupt `answers.json` raises `CandidateProfileInvalid` and is left untouched. `save_answer` never modifies `profile.json`.

## Tests

```bash
uv venv .venv-task --python 3.12
uv pip install --python .venv-task/bin/python -e packages/core -e packages/candidate pytest
.venv-task/bin/python -m pytest tests/candidate
```
