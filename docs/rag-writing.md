# Retrieval-backed application drafts

Jev classifies the complete application question. Confirmed contact values and
simple saved answers use the existing deterministic resolver. Complex experience
questions and cover-letter text route to `anthropic/claude-opus-5.5` through
OpenRouter, after retrieving candidate evidence and the relevant job description.
Jev then checks the draft's claims and whether it answers the entire question.
Ambiguous narrative evidence checks escalate to a separate structured Opus review;
unsupported or incomplete answers remain held with specific missing details. The
receipt keeps Jev's original probabilities and the review verdict separately.
If structured review finds unsupported or incomplete wording, the writer may make
one correction using the same evidence and the specific review issues. That draft
must pass fresh Jev and Opus checks. Missing facts, conflicts, invalid citations,
provider failures and exhausted budgets do not trigger an automatic rewrite.

The initial knowledge source is the canonical profile's verified resume facts.
The private Supabase `imx_knowledge` schema stores a projection of those facts,
job descriptions, optional style samples and their pgvector embeddings. Candidate
IDs, normalized application URLs, source versions and content hashes bind the
evidence. Current local verification remains authoritative: stale, changed or
revoked facts cannot become usable merely because they remain in the index.

Candidate facts support personal claims. Job descriptions support employer and
role statements. Style samples affect tone only. Missing evidence is not evidence
of absence: an ABM question mentioning Demandbase or 6sense requires a confirmed
platform history or an explicit negative answer, not a guess from general B2B work.

## Commands

Run from the repository. The connection file is a private JSON object containing
`host`, `port`, `dbname`, `user`, `password` and `sslmode`. Keep it out of source
control. The OpenRouter key stays in the existing private environment file.

```bash
uv run python scripts/rag_answers.py index-profile \
  --env-file env.local --connection-file /absolute/private/connection.json

uv run python scripts/rag_answers.py index-job \
  --listing-file /absolute/private/job-listing.json \
  --env-file env.local --connection-file /absolute/private/connection.json

uv run python scripts/rag_answers.py draft --cover-letter \
  --listing-file /absolute/private/job-listing.json \
  --env-file env.local --connection-file /absolute/private/connection.json \
  --output /absolute/private/drafts/cover-letter.json
```

To index every resolved Saved job at once before a preparation batch, run
`uv run --no-sync python scripts/index_saved_jobs.py --inventory /absolute/private/application-urls.json --env-file /absolute/env.local --connection-file /absolute/private/connection.json --receipt /absolute/private/receipt.json`.
It indexes only listings whose stored description completeness is FULL, under the
exact application URL the batch will use, so retrieval scope matches at fill time.
Unchanged descriptions need no new embeddings; the receipt records counts and
per-listing status codes only.

`--home` and `--candidate-id` select the canonical profile. `--listing-file` accepts
an existing `JobListing` JSON snapshot with a full description and provenance.
For an experience answer, replace `--cover-letter` with `--question "..."`.
Drafting writes a private JSON receipt and, when READY, a text file beside it.
It opens no browser, creates no application record and submits nothing. NEEDS_INPUT
receipts preserve the exact missing detail. Draft receipts are not inspected forms
and cannot be sent directly to the browser as application packets.

To add personal history later, supply an array of user-confirmed facts with `id`,
`key`, `value` and optional `evidence`, then run `import-facts --file ...` followed
by `index-profile`. Importing means the user confirmed the file; it does not infer
verification from arbitrary scraped or model-generated text. Replace a fact by
its stable ID when correcting it. For tone, `index-voice --file sample.txt` indexes
a professional writing sample as style-only evidence. The initial writer uses
the user's requested direct style; more writing samples can make it more personal.

## Runtime configuration

The CLI accepts `--rag-connection-file /absolute/private/connection.json` alongside
its existing AI-routing flags. The service uses
`IMX_SERVICE_RAG_CONNECTION_FILE`, requiring AI routing and an absolute path.
Configuration validation does not contact providers. A configured retrieval
failure becomes missing input; the writer does not silently use a different source.
File uploads still require a supplied or explicitly approved document.
Runtime retrieval, writing and review share one bounded provider budget. Indexed
facts are selected using both the actual question and the scoped job description;
unchanged indexed sources do not require new embeddings.

Before drafting, the resolver compares the selected facts only with canonical facts
that could be about the same subject. That means a global or negative claim ("never
used X", a false experience flag), the same single-value key with another value, the
same experience group, or an ungrouped bullet that names the same employer, client,
project or tool (a capitalized name, including at the start of a bullet, that is not a
title, job function, company suffix or leading verb). Independent bullets about different employers, budgets, team sizes
or skills are not compared, so they no longer send a narrative to the Opus
evidence review. A real contradiction about the same subject still holds or
escalates as before. Each Jev verdict is cached per runtime under the exact fact and
its comparison set, so a second narrative on the same form reuses it (the
consistency trace lists `cached` keys).

## Verification

Mocked tests cover isolated retrieval, changed and revoked facts, source separation,
embedding contracts, routing, question completeness, citations and writing limits.
Live evaluation receipts and sample drafts are private under `.imx/rag-writing/`.
Record the exact model, prompt version, route probabilities, retrieval timing,
source references and known cost for each evaluation. A successful unit test does
not replace a live retrieval and writer test.
