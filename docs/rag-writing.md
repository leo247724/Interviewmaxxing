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
by `index-profile`. The import lists and does not import a fact whose own evidence dates or
places it elsewhere, and skips rows the story index marked `superseded`;
`remove-facts --ids a,b` removes facts from the profile by id (then `index-profile`). Importing means the user confirmed the file; it does not infer
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
`eval.md`, prompt `no-ai-slop-v1`, `v2` since round 5): lead with the point; cut throat-clearing openers,
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
(`link_story_to_role`, prompt `story-role-link-v1`, `v2` since round 5, purpose `story_role_link`), accepted
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

## Round 3: motivation as alignment, output budgets, screener evidence, enumerations

**Motivation narratives.** Interest, motivation and "why us" questions are written under
their own writer purpose, `motivation` (at most eight sentences, one or two paragraphs,
trace stage `motivation_narrative` with status `MOTIVATION_PURPOSE`). The reason the
draft gives is the alignment between the job description's requirements or priorities
(cited job evidence) and the applicant's own experience (cited facts and story passages):
two or three named requirements and the matching work, employer and period. The writer
never demands, invents or implies a personal reason, familiarity with the company or
enthusiasm the evidence does not carry, and never returns `NEEDS_INPUT` for the lack of
one; the draft must cite both namespaces. One reusable statement, `career_motivation`
(two or three sentences the person writes once about what they look for in a role,
`docs/simple-answers.md`), is stored as a verified user-stated fact; the resolver adds it
to a motivation field's evidence whether or not retrieval surfaced it, so the writer may
cite it as a reason. (Round 4 tightened this: the reason must be the applicant's own,
a cited story passage or the statement; see below.) The review prompt accepts the
alignment as a complete frame.

**Output budgets.** The high-effort writer had reached its output token limit: with
`reasoning.effort` OpenRouter reserves about 80% of `max_tokens` for reasoning, leaving a
3000-token limit some 600 tokens for the cited JSON answer. Narrative calls (`write`,
purposes answer, motivation and cover letter, and the no-slop rewrite) now send an
explicit `reasoning.max_tokens` budget by effort (`REASONING_BUDGET_TOKENS`: low 1024,
medium 1536, high 2560, xhigh 5120, max 10240; OpenRouter maps a token budget to an
effort level for models without one) and a request `max_tokens` of that budget plus the
purpose's answer allowance (`ANSWER_TOKENS`: 2000 for an answer or motivation, 3000 for
a cover letter or a rewrite, bounded by the writer's `max_tokens`), so the answer keeps
its whole room after reasoning; Anthropic models require `max_tokens` above the reasoning
budget, which this satisfies. A `finish_reason` of `length` is retried once at the same
effort with a larger budget (`RETRY_REASONING_FACTOR` 1.5, `RETRY_ANSWER_FACTOR` 2); the
field never holds on the first cut. Each call's receipt status (`OUTPUT_LIMIT`, `OK`) and
the draft trace's `attempts` list (attempt, status, finish reason, reasoning budget,
`max_tokens`) record what happened; a second cut holds with "reached its output token
limit twice", and a retry the call budget refuses holds naming both reasons. Reviews keep
`reasoning.effort`: their verdicts are short. The cost reservation of a high-effort call
grows from 0.06 USD plus the body to 0.09 (answer) or 0.11 (cover letter) plus the body,
and to 0.16-0.20 on the retry; the per-form allowances are unchanged.

**Screener evidence.** The fact-grounded yes/no, choice and select-all screeners (prompt
`experience-screener-v2`) read two more kinds of evidence through the same decisions: a
derived `years_experience.<area>` fact with a value of one or more states that the
applicant worked in that area for that many whole years (derived from the dated resume
roles that name it), so it establishes experience in the area, including having managed
or used the platform or tool the area names; and a story fact (source `story:<chunk>`)
states the employer type it names (an SEO agency, a paid media agency) as the environment
the applicant worked in and the platforms or tools it names as the applicant's own work.
The per-option `source_o<i>` decision maps them onto the options, so "Which paid media
platforms have you directly managed? (Select all that apply)" selects Google Ads from
`years_experience.google_ads = 3` and Meta Ads from a story fact naming it, and "Have you
worked in a performance marketing agency environment?" answers from a story fact naming
the agency (Jev decides whether the named kind is the asked kind). Choice and select-all
screeners retrieve with the question plus their option labels (`_screener_query`), so a
fact naming an option is retrieved even when the question does not name it.

**Enumerations.** A question asking to enumerate or count the applicant's teams, reports,
clients, campaigns or tools (`_enumeration_question`: "How many direct reports do you
currently manage, or have you managed…", "Which platforms have you managed?", "List the
tools…") is written from the facts at hand: the writer receives a guidance rule
(`ENUMERATION_GUIDANCE`, the `guidance` list in the request, never a factual source) to
present each item the facts state with its size, employer and dates, each cited, not to
return `NEEDS_INPUT` for completeness, and not to use totality words (all, every, only, in
total, total across roles, altogether) unless a fact states the total; the required-details
check and the review prompt accept the non-exhaustive list as complete. A draft that still
claims a total the facts do not state gets the one corrective rewrite with that as its
feedback (draft trace status `TOTALITY_REJECTED`); a fact that states the total allows the
word.

## Round 4: fact provenance, the applicant's own reason, bounded comparisons, reviewed rewrites

**Story facts are extracted, not confirmed (H5).** CONTRACTS 3: `VERIFIED` means the
person stated or confirmed the fact; a run clock is neither. `extract_story_facts` now
writes every story fact `UNVERIFIED` (no method, no time), and only sentences in the first
person singular (I, my, me) or resume-style sentences opening with a verb become facts;
"We grew ARR 3x", "our team of 6 closed the deal" describe the team's work, stay in the
story chunks as narrative evidence and are counted in the receipt's `skipped` list as
`plural_subject_only` (the analysis still reads them as the story's actions). An
unverified fact is never writer, screener or retrieval evidence (`verified_facts()`,
`index_candidate` and the resolvers use verified facts only), so a story that has not
been confirmed contributes its passages, not facts.

**Confirming facts.** `--facts-review` writes, beside the review JSON and Markdown, a
`<name>.confirm.json`: every unverified fact of the run (story facts and per-area years)
in the facts-import format (`id`, `key`, `value`, `evidence`). Delete the rows you do not
stand behind, then run `uv run --no-sync python scripts/rag_answers.py import-facts --file
<that file>` and `index-profile`. The import records the confirmation (`VERIFIED`,
`USER_STATED`, provenance `user:confirmed fact import`), and the index script keeps a
confirmed fact by id on every later run (`kept_confirmed` in the receipt): it never
replaces or downgrades it, while an unconfirmed fact it produces again stays
`UNVERIFIED` and one it no longer produces is removed. Fact ids are content hashes, so an
unchanged sentence keeps its confirmation across re-runs; a corrected sentence is a new
fact to confirm. The review Markdown shows each fact's status.

**Years of experience (H4).** `timeline.py` derives an area only from a role's title
(the whole role: "PPC Specialist" dates PPC, paid search, paid media, digital marketing for
its full span) or from a bullet that states its own duration ("ran paid social on Meta
Ads for 18 months", "managed Google Ads for over 3 years", "owned SEO from 2021 to
2023": that duration, capped at the role's length; `stated_duration_months`). A bullet
that merely mentions an area ("piloted TikTok Ads in Q4") dates nothing, and linked
stories never date an area (their tools are the story's evidence, not a timeline). The
total across dated roles stays `VERIFIED` (`USER_CONFIRMED`: it only restates the
person's confirmed role dates); each per-area fact is `UNVERIFIED` until confirmed
through the import above, with its basis in the evidence ("Glaze Agency: title, 24
months"; "Crumb & Co.: a bullet stating 36 months"). The numeric screener therefore
answers "How many years of X" from a per-area fact only once the person confirmed it.

**The applicant's own reason (M7; superseded in round 5, addendum 2).** A motivation narrative needs, besides the
alignment, the applicant's own reason: a retrieved story passage about this kind of work
or the `career_motivation` statement. With neither, the field holds before any writer
call ("Motivation answer needs the applicant's own reason: a story about this kind of
work … or a career_motivation statement …"). A draft whose sentences cite neither gets
the one corrective rewrite with that feedback (draft trace `MOTIVATION_UNCITED`), then
holds.

**Bounded comparisons (M8).** The consistency comparison set (40 for Jev, 24 for the
review) is tiered: facts with the same non-additive key as a selected fact, global claims
("never", "throughout my career") and explicit negatives come first, then the rest by
how many selected facts they compete with; a contradiction in one slot can no longer fall
below the bound behind many additive bullets about the same subject (`tiered_first` in
the consistency trace).

**Reviewed rewrites (M9).** `check_rewrite` keeps each draft sentence's exact citation
set (fact ids and job evidence ids) together on one rewritten sentence: a set that is
split, recombined or moved rejects the rewrite (`REJECTED_MOVED_CITATION`), so a metric
cannot travel to a sentence cited by other facts; sentences citing the same set may
still merge. Every humanized draft is grounded again *and* independently reviewed
(`force_review`), not only at an uncertain score or after a corrective rewrite.

**Smaller items.** An unlinked story's facts carry a span only when its stated years are
adjacent (2022, 2023, 2024) or the story states an explicit range ("2019 to 2023");
years stated apart ("in 2019 … by 2023") date nothing (L8, `stated_year_span`). The
printed receipt carries counts only: `stated_year_count` per story and, for the derived
facts, count/verified/unverified; the values stay in the private review files (L9).
Story chunks still stand in for voice samples when the profile has none (L10): the
writer prompt keeps them style-only and the humanizer's lexical guard (no new number,
name or claim) applies, so a borrowed phrase cannot become a claim. The form allowance
(`allow_form`) is granted once per step (application id and form fingerprint) per
runtime; a re-resolve of the same step grants nothing more (L11).

## Round 5: story linking and dating, restated motivation, dropped story evidence

**An employer-name link needs the company's distinctive name.** A story used to link to
the resume role whose company shared any one distinctive-looking word with it: on the
live index a story headed "Growth Marketing Specialist, <another company>" linked (by
`employer_name`, confidence 1.0) to a resume role at "<Shop> Growth <Solutions>" through
"Growth", and its facts and chunk headers carried that role's 2023-10 to 2024-02 instead of
2019-2020 (the tests use the exact pair, with fictional story text). Now
`match_role_by_name` links only when the story names the company distinctively
(`names_company`): every distinctive word of the company's name (`company_tokens`) appears
in the story's heading, text, employer phrase or product name, or its full name appears as
consecutive words (trailing legal suffixes such as Inc or LLC may be left out,
`company_words`). Common business words (Growth, Solutions, Marketing, Consulting, Law,
Media, Digital, Group, Inc, Partners, Strategy, Creative, Tech and the like) are never
distinctive, so one of them never links, and a name made only of them needs the full
name. Two matching roles are still no match. The Jev link decision (prompt
`story-role-link-v2`) is also told that a shared common word does not make an employer
the same.

**Headings state periods.** The years and periods a story states are read from its heading
as well as its body (`stated_years`, `stated_periods`), and a range is read as a period
with its start and end months: "Aug 2019 - May 2020" is `2019-08 to 2020-05`, "Jun 2025 -
Present" is `2025-06 to present` (open-ended), "Mar 2024 - May 2025" is `2024-03 to
2025-05`; "08/2019 - 05/2020", "2019-08 to 2020-05" and "2019 to 2023" (a years-only range
keeps the earlier `2019–2023` form) are read too. A heading without a range that names one
year, or adjacent years, states that year or span ("Growth Marketer, Acme (2019)"); a year
in the body, or years stated apart, is never a period by itself, and a range whose end
comes before its start is ignored. An unlinked story carries its heading's period, else
the one range its body states (`dating_period`; several different ranges in the body date
parts of the story, so then the year rules of rounds 2 and 4 apply). The review shows the
period and where it was stated ("period the story states: 2019-08 to 2020-05 (in its
heading)"), and "years the story states" now includes the heading's.

**A stated period that contradicts a link rejects it.** When a story's heading states a
period (`story_period`: a range, one year or adjacent years) that shares no month with the
proposed resume role's dates, the story is not that role's account: `build_story_index`
does not link it, it carries its stated period in its chunk headers and facts
(`period_source: story`), and the review names the mismatch ("not linked: <title> at
<company>, <role dates> (proposed by <method>) does not overlap <stated period>" and a
**check** note); the receipt counts `link_period_mismatches` and marks the story row
`link_rejected` (method, resume role id and reason, no names or dates). An open period
("Present") runs to today, like a current role. Before the Jev link decision, the roles
the story's stated period does not overlap are left out (`excluded_by_period` in its
trace), and with none left no decision is asked (`NO_OVERLAPPING_ROLE`); a name link that
the period contradicts is traced `PERIOD_MISMATCH` and then rejected by the index. Only
the heading's period can reject a link: a range in the body may date a part of the work (a
pilot, the years before), and a year the body states outside the role's dates keeps the
round-2 rule (the resume dates win, `stated_year_outside_resume_role` is flagged and the
sentence stating it yields no fact).

**Markdown splits at H1 and H2.** A Markdown or text document with one H1 and H2
sections used to be read as one story whenever its H1 read "Stories NN - ..." (numbered
mode ignored the unnumbered H2s) or an H2 was long or ended with a colon. Now every H1 and
H2 (`#`, `##`, or a setext `===`/`---` underline) starts a story, numbered or not and
whatever its wording, like a heading of any level in a .docx; an H3 or deeper stays inside a
numbered story as before. A heading directly followed by another heading with no text
between (a document title over H2 stories) is a section title, not a story; any other
heading without text is still an error, and a heading longer than 400 characters is one
too. `#hashtag` lines are text (a heading needs a space after its hashes), surrounding
emphasis marks are dropped from heading words, and a title's `|` becomes `/` (the chunk
header's field separator), so the story, its chunks and its facts keep one title and story
id. Setext underlines and thematic breaks (`***`, `---` after a blank line) are separators,
not text.

**Motivation is restated, never pasted.** The person's `career_motivation` statement had
been pasted verbatim into every "why us" narrative. The writer prompt now says: a fact keyed
`career_motivation` is the applicant's own statement of what they look for; restate it in
different words each time, with the same meaning and no new claim, never copy more than 12
consecutive words of it; vary sentence openers and never begin two consecutive sentences
with the same phrase or with "In that same role". The humanizer holds the lexical check
(`ai/humanize.py`, `MAX_QUOTED_WORDS = 12`, `quoted_run`/`quotes_statement`, case and
punctuation ignored): the resolver applies it to the writer's draft before grounding, and a
draft quoting more than 12 consecutive words of the statement gets the one corrective
rewrite with that instruction (draft trace `STATEMENT_QUOTED`), then holds if it quotes
again. Every deterministic finding of a draft (an uncited motivation reason, a quoted
statement, a totality word) now goes into that one rewrite together (`rejected_for`). In
the no-slop pass (prompt `no-ai-slop-v2`) a quoted statement is a lint finding
(`quoted_statement`) and a rewrite that quotes one is rejected
(`REJECTED_QUOTED_STATEMENT`, the grounded draft is kept); two consecutive sentences with
the same three-word opener are a `repeated_opener` finding. Traces carry pattern names
and counts, never the statement.

**Dropped story evidence stays dropped (review pass 5, M1 and M2).** A story passage and
the facts extracted from it are one piece of evidence judged twice (the chunk against its
related facts, the fact against its competitors). When the story consistency check drops a
chunk, the story facts sourced from it (`story:<chunk id>`) or stating one of its sentences
leave the field's evidence too; when the fact consistency check drops a story fact, the
chunks that carry its sentence (its source chunk, a summary chunk repeating it) leave too.
The propagated ids are traced under `story_evidence_dropped` with `propagated_from`, and the
field continues with what remains. A strong evidence review cached for the candidate
revision used to answer every later field before any comparison, so a story fact dropped
for the first field reached the second field's writer; a field that selected a story fact
now runs its comparisons (on the cached Jev verdicts, without a new request), drops the
story fact again, and takes the cached review instead of a second one
(`consistency_cache` with `dropped_story_fact_ids`). This concerns story facts with
`story:` provenance that are verified; a fact the person confirmed through the import has
`user:` provenance and is canonical evidence, and unconfirmed story facts are never
evidence. The story verdict cache (L4) stores a verdict, its score and its asks flag,
only when two slots fit (`max_cache_entries` 0 and 1 cache nothing instead of failing) and
counts it as cached only when both halves are present.

**Addendum 2: fit is given, the writer builds the case.** The owner vets every saved job
before it enters the pipeline, so every one fits by definition. For cover letters, motivation
answers and narrative answers the writer prompt now says so (`FIT_GIVEN_RULE`): map the
posting's requirements to the applicant's experience, concretely and affirmatively; never
hedge ("while I have not…", "although my background is in…"), never add a disclaimer about a
requirement the experience does not cover, never comment on fit; a requirement no fact or
passage supports is simply not mentioned, and every claim still needs its citation. The
resolver enforces it in code: a draft with a hedge, a disclaimer or a fit comment
(`fit_hedges` in `ai/humanize.py`: concessions about the applicant, volunteered gaps,
"a quick learner", "I would be a strong fit") gets the one corrective rewrite (draft trace
`FIT_HEDGED`), and the no-slop pass lints the same patterns (`fit_hedge`). Round 4's M7 is
superseded: a motivation answer's reason is the alignment between the posting and the
applicant's experience, in the applicant's voice, restating `career_motivation` when it is
set; no story passage or statement is required any more, and the field holds only when no
fact relates to the posting at all. The cover-letter prompt no longer asks for missing
experience; it returns `NEEDS_INPUT` only without a real job description or any related
fact. The independent draft review judges grounding and consistency only: never fit,
sufficiency of experience or coverage of the posting (a requirement the draft leaves out
is not an issue; it no longer returns `INCOMPLETE`), and the Jev completeness check and the
required details say the same for cover letters and motivation questions.

**Addendum item 7: case-study questions.** "Calculate CPA and ROAS for each channel. Based on
this information, respond to the above question" asks for a computation over data the form
shows with the question, not for the applicant's history, and used to hold as "No verified
fact or saved answer answers this question". A WRITER-routed text question whose wording
asks to calculate, analyse or respond to given data (`case_analysis_question` in
`ai/case_analysis.py`) is now admitted whatever its candidate source scope and answered under
the writer purpose `case_analysis` (`CASE_ANALYSIS_SYSTEM`): from the question's recorded
wording and `section_context` only (`data_evidence`, cited by `form:<sha256>`), computing every
requested metric with its working shown ("Search CPA = $5,000 / 100 conversions = $50") and
answering the follow-up from those results, citing no candidate fact and making no personal
claim. The working is checked in code (`check_working`: each `… = c` computed correctly from
numbers the data states or earlier results, no other number the data does not state; one
corrective rewrite, then a hold), Jev grounds each sentence in the data and checks
completeness (an uncertain score goes to the independent review), and the answer's
provenance is the new `GENERATED_FROM_QUESTION` source (CONTRACTS.md), which packet
validation re-derives from the inspected field. When the recording carries no data (fewer
than four numbers) the field holds before any writer call with "The table referenced is not
in the recorded question". Today the inspector records headings as `section_context`, not the
table, text or image alt that precedes a question in its block: that part of item 7 belongs to
WP1's `inspect.js` (requested through the lead); until it lands, case questions hold with that
reason instead of the misleading "no verified fact".

**Addendum item 8: confirmed facts that contradict their own evidence.** Three confirmed facts
placed work from a story dated Aug 2019 - May 2020 at a resume role dated 2023-10 to 2024-02:
they came from a confirm file written before the linking fix and imported afterwards.
`import-facts` now lists and does not import a fact whose own evidence contradicts it
(`fact_self_contradictions`: the period its evidence states, such as a story heading's range,
shares no month with the period its value states, or its evidence links or names a resume
employer other than the one its value names; reason codes only, no values); the index script
marks the confirmed facts of a story whose resume link changed, or whose story left the
document, as `superseded` rows in the next `.confirm.json` (`superseded_story_facts`; the review
lists them with the removal command, the receipt their ids), which the import skips; and the
independent review's hold message ends with the profile fact ids it referenced
("… (facts: sf_…, sf_…)") so the person can remove them with `remove-facts`.

**Re-indexing.** The candidate's current documents are unchanged by these rules when
their stories state no heading period and their name links use a distinctive name; a
re-run shows any change in the receipt's `link_period_mismatches`, `linked_stories` and
the chunk ids.
Chunk ids change for a story whose heading states a period and whose link is rejected (its
header carries its own period), and for a title containing `|`.

## Round 6: the cover letter the owner wants

Three live letters (2026-09-25) graded C / B- / D against the owner's rule and C-range on
no-AI-slop. They read the posting back to its authors, opened and closed with stock lines,
argued from years counts and resume/story twins while the best results never reached the
writer, dropped every story passage, and one named the listing source's company. The owner's
rubric (`.imx/rag-writing/cover-letters/RUBRIC.md`, HARD and SOFT lines) and the
open-career-skills cover-letter rules (MIT) are now the specification.

**The target employer.** `rag_answers.py draft --application-id` (or `--job-id`) takes the job
record from the application store, as the runner does; a listing's `company` comes from its
source (Getro named "Coda" for Superhuman). The writer names the employer as `job_evidence`
names it, the review rejects a draft that names it otherwise, and a letter naming the metadata
company where the description names another gets a corrective rewrite (`EMPLOYER_NAME`).

**Retrieval per key requirement** (cover letters and motivation answers). Each requirement
line of the description (`_requirement_lines`: sentences with a requirement cue, each once) is
an input of the same embedding request and its own hybrid query. `select_requirement_facts`
gives each requirement its best fact before any gets a second (at most two), preferring a
fact that states a figure within windows of three hits, leaves out a fact stating the same
claim as one already chosen (`same_claim`: a shared figure and 40% of the shorter claim's
content words, or 60% without figures: a resume bullet and the story fact retelling it), keeps
at most one years-of-experience fact and none below the posting's ask for the same thing
(`10+ years of growth marketing` against `7 years of growth marketing`), then fills from the
whole description's ranking. A cover letter takes up to 12 facts (`max_cover_letter_facts`),
other narratives 8. The receipt's `fact_selection` records the ids per requirement and the ids
left out by reason, never text.

**Job passages.** A cover letter or motivation answer gets the whole description when it has
at most five chunks, else the five with the most requirement-like sentences, in the
description's order; a generic question no longer picks the salary and EEO chunk by its own
words. Specific questions keep their question-ranked chunks.

**Story passages are the spine.** A passage is dropped only at a "free of contradiction"
score of at most 0.05; the uncertain band goes to one independent Opus review for all such
passages with their comparison facts (`STORY_REVIEW_QUESTION`, cached per runtime), which keeps
those it does not find contradicted; without a reviewer or when the review fails, they are
dropped as before. Retrieval ranks first the two passages that best match the posting's first
priority (`story_priority_ids`), and the writer draws the proof from them.

**The writer** (`COVER_LETTER_RULES`, RUBRIC.md line by line): 280-380 words (ceiling 400) in
a greeting line ("Dear Hiring Manager," unless the posting names a person), a two-to-three
sentence hook whose first sentence carries a digit or a named problem, one proof told from a
story passage as constraint, change and result with its tradeoff (never two headline metrics
from different campaigns), three to five sentences on one thing only true of this employer
from the description with a first step, and a two-sentence close with the profile link (the
transient `contact:links` entry from the identity's LinkedIn and website, never packet
provenance) and a confident offer to talk; no gratitude, no application line, no fit
commentary, no tool inventories, at most one sentence restating the posting, the banned words.
`NEEDS_INPUT` only without a description, without any related fact, or when nothing specific
to the employer can be named.

**What the code checks** (`_letter_findings`, corrective rewrites; a letter still failing on
its third draft holds): the greeting line (`GREETING`), 280-400 words in 4-6 paragraphs
(`LETTER_LENGTH`), a hook whose first sentence cites the applicant's work and carries a digit
or a story's problem (`OPENING`), a close of exactly two sentences, the profile link (cited)
and an offer to talk, with no stock courtesy (`CLOSING`), a cited story passage when one was
supplied (`STORY_MISSING`), at most one job-only sentence (`JOB_RESTATED`), the employer's name
(`EMPLOYER_NAME`). A review that rejects sentences asks the next draft to drop them rather than
rephrase them (`DROP_REJECTED_FEEDBACK`), and the writer never borrows the posting's wording into
a first-person claim, never assesses the applicant and states a tradeoff only as a source does.

**One review per draft.** Each cover-letter draft gets one independent review
(`letter_review`, `LetterReview`): its grounding verdict, as the draft review, and its grade
against the rubric's HARD lines (`LETTER_RUBRIC_LINES`) together. A grounding failure is a
corrective rewrite; a failed rubric line gets one improvement draft (`RUBRIC_PASSES`), held to
every draft check and its own graded review, and dropped when it fails any of them, so the
grounded letter stands and the rubric never costs a letter. Issues still open after the pass
(or after the no-slop rewrite's own review) stay in the trace and the note ("rubric review: N
issue(s) remain"; "rubric review passed" otherwise). Jev's completeness question is scoped to
the letter's shape, and a letter's first step is a plan, not a claim of fact, supported by
cited work and a cited priority.

**The humanizer** (`no-ai-slop-v3`). A draft whose lint is clean is kept as it is (`CLEAN`,
which the rubric accepts); otherwise it is rewritten, at most twice. A sentence citing only job
evidence may be deleted or folded into a fact sentence (its job ids join that sentence's),
never added; fact sets keep M9; the greeting line and the close's two sentences stay. The lint
gives the rewrite the genre's targets: `job_restated`, `stock_opener`, `stock_closer`,
`fit_commentary`, `posting_clause` ("the kind of X that <employer> names", "which <employer>
expects", "as the role asks", more than twice), `connective_tic`, `repeated_dates` (the same
date phrase three times), `identical_paragraph_openings`, and the upstream rules the first
version missed (`portable_sentence`: a first-person line with no name or figure whose content
words are at least 40% generic; `fake_strong_verb`; `empty_adverb` for the eleven often-empty
adverbs; `self_answered_question`; `closing_recap`; `and_fragments`; `colon_case`;
`decorative_formatting`; `synonym_cycling`; "in this article" / "let's dive in"); a greeting line
is not the first sentence. The prompt carries the owner's rule: hedges and fit commentary are
cut, never kept as voice. A rejected rewrite is tried again with its reason
(`rejected_rewrite`), a rewrite whose review fails a rubric line the draft passed is rejected
(`REJECTED_RUBRIC`), the trace names every discarded attempt (`discarded`) and the answer's note
says "no-AI-slop rewrite discarded: …", so a kept original is never silent. The trace keeps each
accepted rewrite's and the final draft's citation ids (`citations`, ids and paragraphs only).

**The owner's voice** (addendum 3). Three posts he wrote in 2017 are indexed as voice samples
(`index-voice`, style only); a letter gets the two most relevant passages (`voice_samples`), for
the writer and the no-slop rewrite alike, never as evidence (they reach neither Jev nor the
review). Both prompts carry `VOICE_RULE`: adopt his register (plain first person, direct
address, short declarative sentences, concrete numbers, a homely analogy now and then, a blunt
aside, confidence without puffery, "the bottom line" once) but never the posts' content or their
blog tics (bucket brigades, "awesome", "insanely", "skyrocket", "explosive", "It's no secret
that...", "You might be wondering:"), which the lint names `blog_tic`.

**Cost and bounds.** Citation ids travel as short aliases on the writer's and the rewriter's
wire (`F1`, `S1`, `J1`, `L1`; a hashed id costs some 45 output tokens per citation) and are
mapped back before any check; Jev grounding carries each cited fact, passage and job chunk once
per request, and grounding and consistency requests are split under 85% of the request bound
(`_decide_batched`). A letter's answer allowance is 6000 tokens, the writer policy allows 8000
and waits 120 s, and a cover letter adds 24 calls / USD 2.50 of reservations to a writer field's
allowance, raising the form's cap by the same (`FORM_LETTER_*`); `rag_answers.py draft` reserves
up to 64 calls / USD 3.00. The lead's target is at most 15 calls and USD 0.60 per letter.

## Verification

Mocked tests cover isolated retrieval, changed and revoked facts, source separation,
embedding contracts, routing, question completeness, citations and writing limits.
Live evaluation receipts and sample drafts are private under `.imx/rag-writing/`.
Record the exact model, prompt version, route probabilities, retrieval timing,
source references and known cost for each evaluation. A successful unit test does
not replace a live retrieval and writer test.
