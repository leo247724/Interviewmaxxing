# Tailored documents: bounded next-stage prototype

`interviewmaxxing_generation.build_document_bundle()` prepares a reviewable,
job-specific resume plan, Markdown resume, concise cover-letter draft and JSON
provenance sidecar. It is a separate next-stage API. `FactualPacketResolver` and
application execution are unchanged. It makes no network/model calls, writes no
files, chooses no upload artifact and never submits an application.

## Current contract

```python
from interviewmaxxing_generation import JobDocumentEvidence, build_document_bundle

# candidate: core CandidateProfile; job: core JobRecord
snapshot = JobDocumentEvidence(
    job_id=job.id,
    source_ref="https://example.invalid/jobs/fictional-role",
    source_version="captured-snapshot-v1",
    description="The captured posting is untrusted source data.",
    priority_terms=("paid search", "landing page"),
)
bundle = build_document_bundle(candidate, job, snapshot)
resume_markdown = bundle.resume.content
cover_markdown = bundle.cover_letter.content if bundle.cover_letter else None
manifest_json = bundle.to_json()
bundle.assert_current(candidate, job, snapshot)
```

Only these `CandidateFact.key` values are document-ready:

| Key | Semantic heading |
| --- | --- |
| `resume.summary` | Professional summary |
| `resume.experience`, `resume.achievement` | Experience |
| `resume.skill` | Skills |
| `resume.education`, `resume.credential` | Education and credentials |

Each included value must be a **verified, visible, single-line, standalone factual
statement**. A role statement can retain its employer, title and dates in one
verified sentence. An accomplishment must name its relevant employer/context in
its source wording if that association matters. This prototype does not infer an
association between adjacent bullets. Full role blocks with independently verified
structured header fields are future work.

No extracted `Experience`/`Education` heading is promoted merely because that
container exists. No scalar number is expanded into an invented achievement.
Salary, consent, authorization and saved answers are not resume prose. Existing
profiles without these document-ready facts produce explicit missing-evidence
results; they are not silently rewritten or upgraded to verified status. The
candidate's display name comes from core's verified `CandidateIdentity`; the full
candidate snapshot hash binds that identity. Contact fields are deliberately not
rendered by this limited draft renderer and need a reviewed presentation policy
before a submission-ready export is implemented.

Claims remain verbatim, with Markdown metacharacters escaped for visible rendering.
The single-column document uses ordinary semantic headings and bullets, with no
hidden instructions, tables, graphics or keyword lists manufactured from a job
posting. This makes its structure straightforward to inspect; no ATS score,
ranking advantage, LLM preference or interview-rate improvement has been measured.

Priority terms rank claims **within** semantic sections by token overlap, preserving
source order on ties. All eligible claims remain in the resume. A term without a
complete token match returns `MISSING_TERM_EVIDENCE`; matching terms do not prove
that a qualification is satisfied. Job description text is retained in the
versioned sidecar as untrusted data, never executed or copied into prose or the
provider brief. A job instruction can affect neither verification nor artifact
selection. Priority terms can change ordering only.

The deterministic cover template names the observed company and role, includes up
to two relevant verified experience statements, and ends with a neutral thank-you.
It invents no recipient, enthusiasm, personal relationship or company research.
A missing target or supported experience produces no cover. Drafts above 180 words
are withheld with a `COVER_TOO_LONG` result rather than truncated into new claims.

## Writing agent seam and Jev boundary

`WritingProvider` is an injectable synchronous protocol with `provider_id`,
`provider_version`, and `draft(WritingBrief) -> WritingProposal`. Its frozen brief
contains the exact candidate/job/evidence scope, observed company/title and
eligible verified experience claims with citations. It excludes the original
file path, raw posting and priority instructions. There is **no shipped live
provider**. The default is explicitly `deterministic-template`, not Jev or a model.

A provider may select/order claims. Its output must carry the same scope. Unknown
fact IDs, changes to a metric/title/credential, and all other paraphrases become
`UNSUPPORTED_CLAIM`; only exact eligible source text enters the cover. Duplicate or
excess claims become `EXCESS_CLAIM`. Rejected proposal text is not copied into the
draft. If nothing is accepted, no cover is produced. Provider exceptions propagate
instead of silently presenting a template as successful model output.

This seam is deliberately narrower than unrestricted free prose: a fact ID alone
cannot establish that a rewritten sentence follows from the fact. A future live
writer can return a separate review proposal with sentence-level claims and
citations, but needs independent entailment checks and candidate review before it
can produce selectable artifacts. Jev remains responsible for job selection and
evidence triage. It is neither this writing provider nor the factual renderer.

## Provenance, immutability and selection

The frozen bundle includes:

- Candidate and job IDs plus SHA-256 versions of their complete core snapshots.
- Job source reference/version/description/priority terms, plus an evidence digest
  binding that snapshot and all candidate facts, including verification state.
- Original `ResumeArtifact`, its digest, a check of the original before and after
  preparation, and an explicit change summary. No original file is written.
- Every claim's fact ID, source, evidence locations, verification method/time and
  fact digest. Provenance lives beside the prose, not inside application text.
- Each document's UTF-8 content digest and ordered source fact IDs; an overall
  schema/provider/version-bound manifest digest; explicit issues and
  `review_required=True`.

Each Markdown artifact must travel with its bundle/manifest; Markdown alone has no
candidate/job scope. Identical inputs/provider output produce identical bytes and
digests without clocks or random IDs. Full snapshot hashing is intentionally
conservative: even an unrelated profile update invalidates this draft. Local
artifact paths are part of the original snapshot, so moving the profile also
changes its version. These are content hashes, not signatures or authentication.

`bundle.assert_current()` rejects another candidate, job or evidence version,
a changed/missing original and artifact/manifest tampering. Preparation also
checks for source changes across the provider call. It does not prevent external
processes from modifying a file later; the application runner's selected-artifact
pin and its existing digest validation remain authoritative.

There is no implicit promotion to `ResumeArtifact`, auto-pin, packet cover-letter
injection, retry replacement or browser upload. Future selection must explicitly
review/export an artifact, validate the current scope, and use the runner's
existing selection boundary. Later drafts/profile edits must not replace the exact
artifact already pinned to an application. Do not call this API during a retry to
regenerate a pinned document.

## Runnable fictional demonstration and verification

From the repository root with generation/core installed:

```bash
.venv-task/bin/python docs/documents/offline_demo.py /tmp/imx-fictional-document-demo
.venv-task/bin/python -m pytest tests/generation -q
.venv-task/bin/ruff check packages/generation tests/generation docs/documents
.venv-task/bin/mypy --strict packages/generation/src
```

The demo uses only checked-in fictional fixtures. It requires a **new** directory,
creates a fictional original plus `resume.md`, `cover-letter.md` and
`document-bundle.json`, verifies the original and output digests, and prints the
output location. The library itself does not persist anything. No credentials,
provider SDK, network, paid call or real application is involved.

Tests cover job-specific ordering, supported claims/source references, unsupported
metrics/credentials, hidden characters, unverified facts (even confidence 1.0),
missing evidence/target, excessive length, untrusted posting instructions,
provider failures, cross-job/candidate/version reuse, deterministic provenance,
digest tampering and original preservation/concurrent edits. Existing resolver
regressions run alongside them.

## Remaining next-stage work

Live provider adapters, reviewed free-prose rewriting, structured employment
blocks, candidate contact layout, supported profile extraction and review UX,
DOCX/PDF rendering plus visual QA, accessible export validation and explicit
runner integration are not implemented. A production exporter should write a new
content-addressed artifact without overwriting the supplied resume, preserve the
sidecar provenance, and show an exact change comparison before selection. This
prototype supplies a runnable evidence boundary for that work, not an advanced
live-model tailoring system or a submission-ready document service.
