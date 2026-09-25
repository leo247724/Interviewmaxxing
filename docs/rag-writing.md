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
escalates as before. Since round 7, ungrouped facts that state the same kind of quantity (money, a duration, a
percentage, a count of one unit) are also compared, unless both name different subjects:
"Managed a $2M annual budget" and "Managed budgets up to $500K" can contradict. Retrieval runs
from up to three worker threads; the knowledge store opens a new connection for every
transaction on the calling thread, so no connection is shared between threads.
Each Jev verdict is cached per runtime under the exact fact and
its comparison set, so a second narrative on the same form reuses it (the
consistency trace lists `cached` keys).

## Stories: the candidate's own account

`scripts/index_candidate_stories.py` reads a `.docx` of the candidate's professional
stories (their own words, deeper than the resume) with the standard library only.
Stories start at a bold or heading line reading `Stories NN - title`; a document without
numbered headings falls back to short bold or heading lines. Each story is analysed
deterministically (`knowledge/stories.py`): employer or client, product built, role,
period, situation, actions, quantified outcomes, tools, skills and the themes it
evidences (leadership, budgets, channels, results, measurement, stakeholders, sales
alignment, automation and AI, creative, strategy, failures learned from, persuasion,
clients and agencies, product and engineering, outreach, geo targeting). It is then cut
into retrieval chunks of about 120–300 words plus one summary chunk per story, each
prefixed with a header line (`Story 01: <title> | employer: … | role: … | period: … |
themes: …`) so a chunk stands alone. Chunk ids are content hashes
(`story:<sha256 of the chunk text>`) and stay the same across re-indexing.

The chunks are indexed under the `story` kind of `imx_knowledge` as the candidate's one
current stories source (source id `candidate-stories`, version = the document's
SHA-256; `--source-id` and `--keep-others` hold a second document). An unchanged
document needs no new embeddings and a changed one replaces the source, exactly like a
job description. The migration `20260924190000_candidate_stories_kind.sql` admits the
kind; without it the database rejects story sources.

Concrete first-person sentences become atomic, user-authored facts: `employment` (role,
employer, period), budgets and team sizes (`experience`), channels and tools (`skills`),
results with numbers (`achievement`), certifications (`education`) and built products
(`project`). A value is the sentence as written plus the story's employer and period in
parentheses; numbers are never rounded, converted or invented, sentences over 600
characters are skipped rather than cut, and a vague sentence yields no fact. Facts are
`VERIFIED` by `USER_STATED` (the candidate wrote them), carry `source = story:<chunk id>`
and stable ids (`sf_<story id>_<hash>`), and are merged through
`LocalCandidateStore.upsert_facts`; the fact projection is re-indexed afterwards so the
factual resolver, the yes/no and choice screeners and the consistency check can use
them. A rerun keeps an unchanged fact's verification time.

```bash
uv run --no-sync python scripts/index_candidate_stories.py \
  --file /absolute/private/stories.docx --candidate default \
  --env-file /absolute/env.local --rag-connection-file /absolute/private/connection.json \
  --receipt /absolute/private/stories/index-<date>.json \
  --facts-review /absolute/private/stories/facts-review.json
```

`--dry-run` parses, chunks and extracts without touching the database or the profile;
`--no-facts` indexes chunks only. The printed and saved receipt carries counts, ids and
hashes; the facts-review file is the full private list for the user's review (revoke a
fact by re-importing it `UNVERIFIED` with `rag_answers.py import-facts`). Neither the
receipt nor an error message contains story text or credentials.

### Retrieval for narratives

For a WRITER-routed field (a cover letter, "why us", "describe a campaign you led", any
question Jev classifies as prose) the resolver retrieves with `narrative=True`. Besides
the facts, the scoped job evidence and the style samples, the store ranks the story
chunks by hybrid similarity to the question, the job title and the indexed description's
key requirements (requirement-like sentences of the job evidence, bounded), in the same
embedding request, and returns up to four (`MAX_STORY_CHUNKS`) with their fused scores.
A bare identity or contact wording (first name, email, phone, LinkedIn, city…) never gets
stories, and a non-narrative retrieval (screeners, exact facts) never does either. When
no explicit `voice` sample is indexed, the two best story chunks double as the style
samples (the stories are the candidate's own writing; style only, as ever), which the
receipt marks `voice_from_stories`.

Story chunks reach the writer in the facts list as `{"id": "story:…", "key": "story",
"value": <text>, "story": {title, employer, period}}` and are cited in `fact_ids`
exactly like facts. Grounding checks every sentence against its cited facts and story
passages; a claim without support holds the field as before. Before writing, each chunk
is compared with the verified facts most related to it by named subjects, kinds of
quantity and global claims (one Jev request for all chunks, purpose `story_consistency`,
prompt `story-evidence-v1`), never with the facts extracted from that same story; a
contradiction holds the field, and verdicts are cached per runtime with the fact
consistency verdicts. Packet provenance keeps citing verified fact ids only, so a draft
that cites nothing but stories is held; the cited story ids and their source versions go
into the answer's note. Traces and retrieval receipts record story ids and scores, never
chunk text.

## Effort by purpose

The writer's reasoning effort follows the purpose. Cover letters, narrative answers and
the humanizing rewrite use the narrative effort: `--writer-effort {low,medium,high}` on
`apply`, `resume` and `prepare-batch` (default `high`; a `prepare-batch --retry`
inherits the recorded value), or `writer_effort` of `build_ai_runtime`. Reviews and
short factual decisions stay at the writer's base effort, `low`. Every writer receipt
records the effort requested; the runner's `provider.budget` event carries
`reasoning_effort` (by purpose, as used) and `writer_effort` (narrative and review, as
configured). A bare `NarrativeWriter` without `narrative_effort` keeps one effort for
everything, as before.

## No-AI-slop pass

After a draft passes grounding (and the independent review when a score was
uncertain), `ai/humanize.py` rewrites it under the rules of
[petergyang/no-ai-slop](https://github.com/petergyang/no-ai-slop) (`SKILL.md` and
`eval.md`, prompt `no-ai-slop-v1`): lead with the point; cut throat-clearing openers,
faux-insight setups, binary contrasts ("It's not X. It's Y."), negative listing, colon
reveals, dramatic fragments, rhetorical setups, superficial trailing `-ing` analysis,
importance puffery, weasel attribution, interpretive metadiscourse, fake-profound kickers
and summary-recap endings, synonym cycling and robotic rhythm, the banned words and empty
phrases, em dashes and formatting slop; active voice, direct verbs, concrete facts, the
candidate's own cadence from the `voice` samples. The rewrite request carries the draft
with its citations, the lint findings and the style samples, not the evidence, and is
bound in code: it must stay `READY`, keep every cited id and cite nothing new, add no
number, stay within 0.6–1.4 of the draft's word count (a cover letter still 200–300
words in 3–4 paragraphs, an answer at most 8 sentences) and fit the field. Then the
cached consistency verdicts and the per-sentence grounding and completeness checks run
again on the rewritten text (with the strong review under the same rule as the first
draft). If any of that fails, the draft that already passed is kept. The deterministic
lint (`lint`) runs on the final text; a residual pattern triggers one more rewrite at
most (`MAX_REWRITES = 2`), after which the text is accepted with the residual recorded.
The trace stage `humanize` lists pattern names and counts, attempts and their statuses
(`REWRITTEN`, `REJECTED_<reason>`, `GROUNDING_REJECTED`, `HELD`), never text; the
receipt purpose is `humanize`. `DynamicPacketResolver.humanize` is off for a bare
resolver and on in `build_ai_runtime`.

## Round 2: story dates, motivation questions, years of experience

**Dates.** A story's facts and chunk headers carry only a period that is grounded. The
indexing script links each story to a resume role (`candidate.experience`): first by a
distinctive company token the story mentions (`match_role_by_name`, generic words such as
"agency" or "law" never match, and two matching roles are no match), otherwise by one Jev
decision over the resume roles with their titles, dates and verified bullets
(`link_story_to_role`, prompt `story-role-link-v1`, purpose `story_role_link`), accepted
only at confidence 0.90 and probability 0.95. A linked story carries the resume role's
dates (`2024-03 to 2025-05`, `2025-06 to present`) in its chunk header (`resume role:`,
`period:`), its summary chunk and every fact's value, and records
`resume_role_id: <experience id>` and `period_source: resume_role` in the fact's
evidence. An unlinked story carries a year only when the story states one
(`period_source: story`); otherwise no period at all (`period_source: none`). There is
never a default year. When a story states a year outside the linked role's dates, the
resume dates are used (the verified profile is authoritative) and the receipt counts a
`stated_year_discrepancies` entry; the review file names the story so the person can
correct the document or the resume. Re-indexing replaces the previous story facts: every
profile fact whose provenance starts with `story:` (or is `derived:experience_timeline`)
that the run does not produce again is removed through `LocalCandidateStore.remove_facts`
before the new ones are merged, so a fact that was dated wrongly disappears. `--dry-run`
links by name only and calls no provider; `--no-links` skips the Jev decision.

A linked story's resume dates are authoritative downstream too: the story evidence
handed to the writer carries a note ("The resume dates this role … ; these dates supersede
any year the passage itself states"), the writer prompt dates the work by them, the
transient stand-ins that grounding and the independent review see carry the same line,
and the story consistency check tells Jev that a difference between the passage's year
and the resume dates is not a contradiction. So a draft that uses the resume dates is not
held by the chunk's own year.

The script also reads `.md` and `.txt` stories (a heading line, "Stories NN - title" with
or without Markdown marks, then paragraphs up to the next blank line; the words are not
changed) and can hold several documents as separate sources (`--source-id … --keep-others`).
Each story fact records its source (`story_source: <id>` in the evidence), and
re-indexing one source replaces only that source's facts. The review file flags a figure
the story states differently from the linked resume role's bullets (team sizes so far)
without resolving it.

**Bounded review evidence.** The independent evidence-consistency review no longer
receives the whole fact store: it gets the selected facts plus the canonical facts that
compete with the most of them, at most 24 (`REVIEW_EVIDENCE_LIMIT`; the `strong_review`
trace records the selected ids, the competing total and the limit). With a hundred-fact
profile the review's 128-record cap is never
reached, so the cap cannot hold a field by itself; the writer still sees at most eight
relevant facts and four story chunks. The Jev consistency check is bounded the same way:
the lexical competition scan still covers the whole store, but Jev compares the selected
facts with the top 40 competing facts (`CONSISTENCY_COMPARISON_LIMIT`, one request; the
`consistency` trace records `competing_total` and `compared`).

**The resume is canonical: contradicted story evidence is dropped, not held.** At
extraction, a linked story's sentence that claims a tenure or duration longer than the
resume dates the role ("almost 2 years" against a 12-month role; `tenure_conflicts`,
minimum months per phrase) yields no fact, like a sentence stating a year outside the
role's dates, and the review file names the phrase and the tenure. At write time the
story consistency check drops a chunk that contradicts a verified fact instead of
holding the field, and the fact consistency check drops a story-derived selected fact
whose verdict fails; both record `story_evidence_dropped` (ids, probabilities, the
reason) and the narrative continues with the remaining evidence, which is also what the
grounding, the independent review and the provenance note see. The field holds only when
the question itself asks about the contradicted point (the same Jev request answers, per
chunk, whether the question is about the role's dates, tenure or figures; `HELD` in the
trace) or when no usable verified fact remains. A resume fact in conflict with another
resume fact still holds as before.

**Pruned years facts.** An area only a linked story names (never a resume role's title or
bullets) becomes a `years_experience.<area>` fact only at two whole years
(`MIN_STORY_AREA_YEARS`); an area the resume itself names needs one full year, as before.
A single tool a story mentions for one year no longer adds a fact.

**Motivation questions.** A WRITER-routed text question whose wording asks about the
applicant's interest, motivation or fit ("What interests you about Acme?", "Why do you
want to work here?", "What draws you to this role?", "Why this company?"; see
`motivation_question`) is written with `purpose="cover_letter"` whatever the
explicit-answer share of its source scope: grounded in the indexed job description and
the candidate's own account, stating alignment facts, never inventing enthusiasm,
circumstances or opinions the evidence does not carry (the cover-letter writer prompt,
grounding and review enforce that as before, and the field needs the indexed job
description). Its confidence is the route's own, and the trace stage
`motivation_narrative` records the decision with the source-scope probabilities. Salary,
relocation, availability, hours, travel and work-authorization questions are not
motivation questions and keep their explicit-answer hold. The knowledge store also ranks
candidate facts by the job's description for such a question, as for a cover letter.

**Years of experience.** `knowledge/timeline.py` derives duration facts from the resume
timeline: `years_experience` (the dated roles merged, overlaps counted once) and
`years_experience.<area>` for every area a role's title or verified bullets name, or that
a story linked to the role names through its tools and skills. Values are whole years
rounded down; an area under a full year yields no fact (the question holds rather than
answering 0); no area exceeds the total. The keys follow the convention the factual
resolver already reads (`years_fact_area`), so both the deterministic years path and the
Jev numeric screener can use them. The facts are `VERIFIED` (`USER_CONFIRMED`, derived
from the confirmed role dates) with provenance `derived:experience_timeline`, ids
`derived_years_experience[_<area>]`, and are replaced on every run like the story facts.
A question about a skill no role names still holds.

## Verification

Mocked tests cover isolated retrieval, changed and revoked facts, source separation,
embedding contracts, routing, question completeness, citations and writing limits.
Live evaluation receipts and sample drafts are private under `.imx/rag-writing/`.
Record the exact model, prompt version, route probabilities, retrieval timing,
source references and known cost for each evaluation. A successful unit test does
not replace a live retrieval and writer test.
