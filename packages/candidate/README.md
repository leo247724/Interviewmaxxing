# interviewmaxxing-candidate

Loads the user's verified candidate profile, supplied resume and saved answers from local storage. It also persists answers the user chose to reuse. It implements the core `CandidateLoader` and `SavedAnswerWriter` interfaces (CONTRACTS.md §3 and §6) and returns core contract types only.

## Construction (for I1 and the frontend bridge)

```python
from interviewmaxxing_candidate import LocalCandidateStore
from interviewmaxxing_core import LocalPaths

store = LocalCandidateStore.from_paths(LocalPaths.from_env())  # profile_dir = IMX_PROFILE_DIR
store = LocalCandidateStore.from_env(env=None)                 # same, from os.environ or a mapping
store = LocalCandidateStore(profile_dir)                       # Path | str; relative → resolved against CWD now

profile: CandidateProfile = store.load(candidate_id)           # CandidateLoader
report: CandidateLoadReport = store.load_report(candidate_id)  # load() plus sources and exclusions
store.save_answer(candidate_id, saved_answer)                  # SavedAnswerWriter
store.exists(candidate_id) -> bool
store.candidate_dir(id) / store.profile_path(id) / store.answers_path(id) -> Path
```

The store holds no open resources and is safe to construct per request. The runner's missing-input step is:

```python
saved = user_input.to_saved_answer(job=job)   # None for AnswerReuse.APPLICATION
if saved is not None:
    store.save_answer(candidate_id, saved)
```

## Files

Under `LocalPaths.profile_dir` (`$IMX_HOME/profile`; outside source control):

```
<candidate_id>/
    profile.json   CandidateProfile JSON, written by the user (never modified by this package)
    answers.json   JSON array of SavedAnswer, written by save_answer (may also be edited by hand)
    resume.pdf     optional location for the resume; resume.path may point anywhere
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
- **Same question, different answers.** Two answers ask the same question when their scope, `job_identity_key`, `job_url`, `semantic_type` and `normalize_text(question)` are all equal. When they disagree, the one with the strictly latest `confirmed_at` is kept and the others go to `report.superseded_answers`. If the latest confirmations disagree, none of that question's answers is returned; they go to `report.answer_conflicts`, so the question is asked again instead of guessed. Values compare type-strictly: `False` ≠ `0` ≠ `"No"`. Text compares with `normalize_text`, and label lists compare as sets. Answers with different scopes or targets are never merged. For example, when a JOB answer and a GLOBAL answer both apply to a job, both are returned and the resolver decides.
- **Errors are actionable.** `CandidateNotFound` names the expected `profile.json` path. `CandidateProfileInvalid` names the file and either the JSON line and column or each schema problem as a location such as `facts[0].verification: Field required` or `[2].scope: Field required` in `answers.json`. Resume problems give the resolved path and what to change. JSON with duplicate keys, `NaN` or `Infinity` is rejected.

## Writing answers

`save_answer(candidate_id, answer)` writes the `SavedAnswer` exactly as given, scope included, to `answers.json`:

- It writes atomically and with owner-only permissions (`0600`). A lock file serializes concurrent writers (the CLI and the frontend).
- It replaces a stored answer to the same question (same key as above). Answers with another scope or target are untouched, so saving a JOB answer can never replace or widen a GLOBAL one, or the reverse.
- Saving an identical answer again is a no-op. Reusing an id for a different answer, or an id already present in `profile.json`, raises `SavedAnswerRejected` and writes nothing.
- It requires an existing `profile.json` (`CandidateNotFound` otherwise). A corrupt `answers.json` raises `CandidateProfileInvalid` and is left untouched. `profile.json` is never modified.

## Tests

```bash
uv venv .venv-task --python 3.12
uv pip install --python .venv-task/bin/python -e packages/core -e packages/candidate pytest
.venv-task/bin/python -m pytest tests/candidate
```
