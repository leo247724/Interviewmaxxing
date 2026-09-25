# Mass-apply readiness (September 24, 2026)

Status of the pipeline for preparing many applications from the Saved inventory,
what was verified today, and the bottlenecks that still limit scale or quality.
Everything below ran **prepare-only**: real Saved application pages were opened and
filled up to the final review step, and nothing was submitted to any employer.

## What is in place

| Piece | State |
| --- | --- |
| Saved inventory | 886 Saved cards; 873 resolved application URLs across 57 backends (`docs/application-url-inventory.md`) |
| Backend catalog | 22 observed (393 jobs), 20 partial (352), 14 blocked (122), 1 manual/email (5) (`docs/application-schemas/README.md`) |
| Simple answers | 27-key map imported and confirmed; live matrix resolves all 27 with zero writer calls in about 1.5–2.7 s |
| Jev routing | Full-form routing v6 frozen; blind heldout: 0 unsafe copies, narrative recall 88% (`docs/dynamic-application-routing.md`) |
| RAG writer | 35 verified resume facts indexed; job descriptions indexed for every resolved Saved job with a FULL description (`scripts/index_saved_jobs.py`) |
| Live drafts | 3 tailored cover letters and 3 complex answers READY; the ABM platform question correctly stops for input (`.imx/rag-writing/evaluation/`) |
| Prepare-only guard | Runner default, persisted per application, enforced again in the store and the browser layer (`docs/application-preparation.md`) |
| Local service | Reloaded with AI routing, OpenCLI and RAG configured; still `TEST_ONLY` for dashboard-started runs |
| Bulk harness | `interviewmaxxing prepare-batch` (`docs/mass-preparation.md`): bounded parallel workers, resumable ledger, readiness summary |

## Real-form pilot before the runtime fixes

Twelve resolved Saved URLs across eleven backends, four headless workers in parallel,
AI routing and RAG on, prepare-only. Every run ended within 1–4 seconds, and none
reached a filled final review legitimately:

| Observed | Backends | Root cause |
| --- | --- | --- |
| "CAPTCHA widget on the page" before any field was filled | Greenhouse, Lever, JazzHR, SmartRecruiters | An invisible reCAPTCHA/hCaptcha badge embedded in the form was classified as a blocking CAPTCHA page. Only submission needs it. |
| "page is UNKNOWN" on a blank or loading page | Ashby, BambooHR, Gem, Workable | Single-page apps were inspected right after `load`, before the form rendered ("Fetching application form"). |
| "page is UNKNOWN" behind a dimmed overlay | Workable | Cookie-consent dialog covered the page. |
| "page is UNKNOWN" on a closed posting | Paylocity | "That job does not exist or is not currently active" was not recognized as closed, so the run stayed retryable. |
| "Prepared to the final review step" in 2 s on the job description | Rippling | The posting's language selector plus an "Apply now" button was misread as a one-field final form. |

Fixes for all five are implemented and covered by mock-ATS tests (see the
"Runtime fixes" section below); the pilot rerun after the fixes is recorded in
`.imx/dynamic-applications/pilot-2026-09-24/`.

## Runtime fixes made today

All are in the generic browser runtime (`packages/browser`) and covered by new
mock-ATS scenarios (`captcha-widget`, `spa-loading`, `cookie-banner`,
`/postings/with-select`, `/closed`) in `scripts/mock_ats.py`:

1. **Embedded CAPTCHA badge no longer blocks filling.** A widget-only CAPTCHA on a
   real form keeps the page an application form with `captcha_pending`; preparation
   fills everything, stops at review and says the CAPTCHA must be solved before
   submission. Submission mode still refuses to dispatch while it is unsolved and
   asks the user to solve it. Text-challenge CAPTCHAs behave as before.
2. **Bounded page readiness.** After navigation an UNKNOWN page is re-read every
   0.5 s until it classifies, until it is stable with no loading indicator, or until
   the settle timeout (15 s). Static pages classify in about 1.6 s.
3. **Cookie-consent dismissal.** One decline-preferred click on a consent banner
   outside the application form, then re-inspection.
4. **Closed-job wording** now covers "does not exist or is not currently active",
   "no longer active/available", "position has been filled" and similar, so those
   runs end `FAILED_PERMANENT` (`closed` in the batch ledger) instead of retryable.
5. **Apply controls are not submit controls.** An "Apply now" button whose form has
   fewer than two fillable fields is an apply control; such a page is a job
   description and the runtime follows it to the real form. A single-field form now
   needs a genuine submit or next control to count as an application form.
6. **Section headings are model context, not question wording.** Enclosing headings
   go into `ApplicationField.section_context` (sent to Jev for subject and
   timeframe) and no longer into `help_text`, so saved answers keyed on the bare
   question wording match again and fingerprints stay stable across sections.

7. **Unlabelled controls take their visible question.** Inputs whose only label was
   a machine identifier (`cards[<uuid>][field3]`, random ids) now use the adjacent
   question text; required markers (`*`, `✱`, "(required)") are stripped from
   wording and still set `required`; radio groups labelled only by their first
   option ("YES") take the preceding question instead.
8. **Choice groups without distinct values are inspectable.** Ashby radio groups
   whose inputs all carry an empty value crashed normalization; they now get stable
   synthetic option values and fill by label.
9. **Apply wording and navigation forms.** "Apply To Position", "Apply for this
   Job", "Apply here/online/today", "Start/Begin application" are followed, and a
   form-submitting apply button is clicked when its form has no fillable field
   (Dayforce); third-party "Apply with LinkedIn/Indeed" flows are never followed.

Also today: profile-URL fields (LinkedIn, GitHub, website) accept a near-threshold
current-versus-historical source score without an extra Jev call; the writer prompt
preserves each fact's exact relationship to the work (consulting is not building,
no invented "in-house"), which turned the held Spoks letter into a first-attempt
READY; and `scripts/index_saved_jobs.py` indexes every FULL job description.

## Real-form pilot after the runtime fixes

The same twelve applications were resumed and 68 further resolved Saved URLs were
prepared through `interviewmaxxing prepare-batch` (three headless workers, AI routing
and RAG on, prepare-only; 80 real applications in total, nothing submitted). Ledger
and summary: `.imx/dynamic-applications/pilot-2026-09-24/`.

| Outcome (80 applications) | Count | What it means |
| --- | ---: | --- |
| Prepared to final review | 6 | Gem (4), one custom site, TriNet Hire: every field filled, stopped at review. |
| Reached the form, stopped for input | 42 | Filled what it could; the holds are listed below. |
| Did not reach a fillable form | 29 | Root causes below; most are fixable runtime gaps, not employer walls. |
| Job closed | 3 | Recorded `closed`; the pipeline cards should be moved. |

Median wall time per application was 4 s; the 95th percentile was 34 s (forms with
narrative questions). Holds on forms that were reached, by frequency:

| Hold | Where | Count |
| --- | --- | ---: |
| Custom widgets: phone country picker, location typeahead, React selects for work authorization, sponsorship, gender, "how did you hear" | Greenhouse (every form), Rippling (every form), Paylocity state, BambooHR state/country | 12 fields on 12 forms |
| Standard screener questions whose wording carried a machine label prefix or a required marker ("cards[…][field4] Are you legally authorized…? ✱", "YES Do you currently live…") so saved answers could not match | Lever, Workable, Dover, Loxo, Rippling, Breezy | 16 |
| Explicit answers by design: desired salary, salary-range acknowledgement, background-check consent, EEO survey blocks, "why are you excited" motivation prose | Lever, Pinpoint, Greenhouse, Ashby, Rippling, Workable | 21 |
| Yes/no experience screeners that need a verified fact ("experience at a digital marketing agency", "hands-on ASO") | Workable, Greenhouse, Rippling, BambooHR | 9 |
| CAPTCHA interstitial before any form | SmartRecruiters (all), Recruitee | 4 |
| Resume upload held as an autofill/parser control | Ashby | 2 |

Why 29 applications never reached a fillable form:

| Cause | Backends (jobs in inventory) | Count |
| --- | --- | ---: |
| Field normalization crashed on radio groups with duplicate option values | Ashby (170) | 6 |
| "Apply To Position" / similar apply wording not recognized, or a form-submitting apply button | Breezy (12), Comeet, PCRecruiter, Jobvite, Dayforce (7) | 8 |
| Resume or contact fields failed fill verification (custom upload widgets, LinkedIn autofill overlays) | Lever, Teamtailor, BambooHR, two custom sites | 5 |
| Bot protection returned HTTP 403 to headless Chromium | Gusto, Jobvite, one custom site | 3 |
| Employer-hosted Greenhouse board (form inside an iframe or a third-party careers site) | Greenhouse `?gh_jid=` pages | 2 |
| Multi-step form with no unambiguous Next control | JazzHR (19) | 2 |
| Still rendering or blank after the readiness wait | Workable, Breezy | 3 |

The Ashby crash, the apply-wording gap, the machine-label prefixes and the required
markers are fixed (items 7–9 above) and re-verified on the same real applications
(see "Re-run after fixes 7–9" below). The custom-widget handler is the largest
remaining piece of work and gates Greenhouse and Rippling.

## Re-run after fixes 7–9

The batch was rerun with the same id (only failed rows retry) and four held
applications were resumed. Latest state of the 68 batch jobs:

| Outcome | Before 7–9 | After 7–9 |
| --- | ---: | ---: |
| Prepared to final review | 5 | 5 |
| Reached the form, stopped for input | 34 | 46 |
| Did not reach a fillable form | 27 | 15 |
| Closed | 2 | 2 |

All six Ashby applications, both Breezy applications and both JazzHR applications now
reach their forms. On Lever the screener questions are asked by their real wording and
the work-authorization and sponsorship saved answers match; the remaining Lever holds
are desired salary (explicit by design), earliest start date (no fact), the state of
residence (near-threshold identity score) and the referral source, whose saved value
"Company career page" has no matching option in Lever's list (LinkedIn, Indeed,
company website, referral, other). That option mapping is a small follow-up.

The 15 that still do not reach a form: bot protection returning 403 to headless
Chromium (3), fill verification failing on custom upload or autofill widgets (4:
Lever with the LinkedIn autofill overlay, Teamtailor, BambooHR file input, one
custom site), employer-hosted Greenhouse boards with the form in an iframe (2),
Dayforce whose apply button sits in a form with a fillable field (2), Comeet,
PCRecruiter and Jobvite apply wordings not yet in the accepted list (3), and one
Workable posting returning HTTP 410 that should be recorded as closed rather than
retryable (1).

## Custom widgets (bottleneck 1) — implemented

Two Opus 5.5 workers built the design that the live probes called for; nothing is
site-specific and every action is a fixed script verified by readback
(`docs/dynamic-runtime.md`, section "Custom widgets"; `docs/dynamic-application-routing.md`,
section "Option equivalence, referral policy and lookup choice").

- **Menu probing at inspection.** Closed comboboxes inside the form (react-select inputs,
  Rippling `div[role=combobox]`) are opened once per document, their own `aria-controls`
  listbox is read, and they are closed and checked unchanged. They then behave like native
  selects. Budget 24 probes / 20 s per page; results cached; `classify` never probes.
- **Select and verify.** Open the recorded way (click, or focus + ArrowDown), filter long
  lists by typing the label, click the option node, then read back: an exact full-label
  display confirms; a shared abbreviation such as "+1" (United States and Canada) needs the
  reopened menu's selected option (`aria-selected`, `aria-activedescendant`, or the option's
  "selected" class). Headless sessions present a Linux Chrome user agent because
  react-select hides those attributes on Apple user agents, confirmed on the live
  Greenhouse bundle.
- **Lookups (`TYPEAHEAD`).** The candidate's own city/state/country is typed, the site's
  suggestions are read, and exactly one strict match is committed; otherwise Jev chooses
  among the suggestions ("Austin, TX" versus "Austin, MN"), the label is typed verbatim,
  and only if Jev is not confident is the person asked with the suggestions as options.
- **Phone pickers.** A `tel` input with a country picker receives `+1<digits>` so the
  widget selects the country itself; digits and dial code are read back.
- **Option equivalence.** When a stored answer does not match an option's wording, Jev maps
  it to the option with the identical meaning ("No" to "No, I will not require
  sponsorship") at the usual 0.95 gate; it never produces a value. The referral-source
  question never holds: careers-page/website option first, then "Other", then a job
  board, then the first enabled option.

Coverage: five mock-ATS replicas (react-select with a body portal and toggling, Rippling
div comboboxes including an ArrowDown-only one, async city/state lookups, an
intl-tel-input phone widget, a held multi-select) and 75 browser tests, plus 78 resolver,
routing and runner tests. Multi-select chips, virtualized lists under OpenCLI, and widgets
inside dialogs remain held for the person.

First live check (pilot 5, prepare-only, 15 resumed applications plus 44 fresh jobs): on
seven of eight resumed Greenhouse forms the phone picker, location lookup and every React
select were filled, leaving only genuine questions (salary, consent, sponsorship phrased
differently from the saved answer, experience screeners). Two regressions surfaced and are
being fixed before the numbers are published here: some Greenhouse forms lose the fill
context after the first fields (evidence `001-context-lost-step-0.png`), and the live
Rippling page's comboboxes were not probed at all (all ten stayed UNSUPPORTED), unlike the
mock replica.

### Live re-check after the Greenhouse/Rippling stabilization (commit bd77c28)

Six applications that had failed on the widget path were resumed (prepare-only):

| Backend | Result |
| --- | --- |
| Workable | Prepared to the final review step (phone picker and drop-zone resume now fill and read back). |
| Rippling ×3 | One prepared to the final review; two stop only on a genuine question (a state-list residence question, a travel-level question, salary). All comboboxes probed and filled. |
| Greenhouse (Reunion) | Every control filled including the dial-code country select, resume attach and React selects; one hold left on the Email field (near-threshold identity score, fix queued). |
| Greenhouse (FirmPilot) | Still stopped on the sponsorship select. A per-field signature diff on the live page found the cause: Greenhouse re-renders the resume uploader a few seconds after the attach (buttons become "Remove file", the submit button's position shifts), and that late change arrives while a later field is being written, so the fill guard reports a changed page. Every field fills when written one at a time. Fix in progress (accept late uploader re-renders confined to the uploader and action controls; compare the submit control by identity, not position). |

## Second wave (same day): what else the workers shipped

- **Reworded reusable questions** (commit 554f715): a confirmed GLOBAL saved answer now answers a
  differently worded question of the same semantic type when Jev judges the two identical in
  subject, timeframe, polarity and answer type; the value still comes only from the saved answer.
- **Yes/no experience screeners** answered from verified facts (YES only when a fact states the
  experience, NO only when a fact denies it, otherwise a specific hold).
- **Residence screeners and current-address fields** (commit 9403251): "do you live in the US",
  state lists and current-location selects are answered from the verified address; the
  near-threshold identity flake on current-address fields is gone; single-choice and numeric
  screeners (budget ranges, counts, certifications) come from facts only.
- **Resume with autofill** is routed as the approved attachment (Ashby); the browser-side
  upload-first ordering is in progress.
- **Eleven more reusable defaults** in the simple-answers map (null until you fill them):
  previously employed here, previously interviewed here, related to an employee, willing to
  relocate, open to other positions, willing to provide references, desired salary, English
  proficiency, available time zones, travel willingness, earliest start date.
- **Batch bookkeeping** (commit 83640e3): prepared applications are linked to their Saved cards,
  closed jobs move to the Closed lane with a dated note, and `interviewmaxxing batch-report`
  groups the remaining holds by category across batches.

Batch report over the 112 real applications run so far: 7 prepared, 79 stopped for input,
23 did not reach a form, 2 closed. Top holds: Ashby resume drag-and-drop (10), Greenhouse
phone country picker (5), Lever resume attach (5), EEO blocks (5, explicit by design),
state/country selects (8), pre-form CAPTCHA (4). Each of these except the CAPTCHAs is owned
by a running worker.

## Availability of the inventory

A read-only HTTP probe of the 873 resolved URLs (no browser, one request each):

| Verdict | Count | Note |
| --- | ---: | --- |
| Page served normally | 705 | Includes SPAs whose content is client-rendered, so "served" is not "open". |
| Bot challenge for plain HTTP | 124 | Workable, Wellfound, Rippling, SmartRecruiters; a real browser passes these. |
| HTTP 403/401 | 19 | Indeed and SmartRecruiters block scripted HTTP; Indeed also needs sign-in. |
| Gone (404/410) or closed wording | 20 | Lever 404 (5), JazzHR 410 (4), Workable 410 (2), Paylocity/LinkedIn/Dayforce closed wording (5), other 404 (4). |
| Network error | 5 | |

At least 2–3% of Saved jobs are already closed; SPA backends cannot be judged without
a browser, so the true share is higher. The batch ledger records `closed` outcomes so
the pipeline can be updated after each run.

## Cost and time per narrative (live, low reasoning effort)

| Case | Result | Wall time | Provider calls | Known cost |
| --- | --- | ---: | ---: | ---: |
| Cover letter, first draft accepted | READY | 25–31 s | 7–8 | USD 0.08–0.15 |
| Cover letter needing one corrective rewrite | READY | 48 s | 11 | USD 0.23 |
| Complex experience answer | READY | 18–21 s | 6–7 | USD 0.07–0.12 |
| ABM platform question without evidence | NEEDS_INPUT | 2.4 s | 2 | < USD 0.001 |
| 27 simple contact answers | READY | 1.4–2.7 s | 4–5 | USD 0.002 |

The Jev fact-consistency check held on compatible facts in half of the narratives and
escalated to a structured Opus review at about USD 0.056 per field. A typical form
with one cover letter and two narrative questions is therefore USD 0.25–0.45 and
60–90 s of provider time, before browser time. Eight hundred such forms would cost
roughly USD 200–360 in provider fees.

## Scope change (evening, September 24): LinkedIn Easy Apply is out

LinkedIn flagged the person's account for unusual profile-data access, so nothing in this
system may open linkedin.com any more: the 78 LinkedIn Easy Apply jobs are applied to by
hand, the finder's LinkedIn source stays off, and sign-in-gated work (WP6) ships on mocks
only. Bottleneck 2 below now covers Wellfound, Indeed, Workday and iCIMS only.

## Pilot 7 (evening): 40 fresh jobs, prepare-only, and what every hold turned out to be

Run against j-workspace during the WP5 merge (ce7c0ce → 855dd2b), four browser slots, nothing
submitted. The runner now records a `routing.trace` event per resolved step (every field's route,
source scope, semantic type and their probabilities plus the resolver's decision traces) and
`failed_fields` on `application.failed_retryable`, so each hold below was read from the store.

| outcome | count |
| --- | --- |
| prepared (Lever) | 1 |
| needs_input | 33 |
| failed_retryable (Jobvite apply page not recognised; Teamtailor hidden import input) | 2 |
| already recorded | 4 |

Median 11 s per form; provider cost USD 1.46 over 34 applications, almost all of it narratives.
The 149 holds by cause and owner:

| cause | holds | evidence | owner |
| --- | --- | --- | --- |
| Saved answer of the same type not applied (work authorization, sponsorship, veteran, race) | 12 | `question_equivalence` offered the one typed answer plus nine untyped ones; NONE 0.60, BELOW_GATE 0.83–0.94 | WP2 round 6 (type-filtered candidates) |
| Whole form lost to the 60 KB request bound (Appspace, 13 fields including First Name) | 13 | a 240-option dial-code select serialised in full | WP10 (option caps, batched requests) |
| Ashby required resume "purpose is not a verified attachment" with APPROVED_DOCUMENT 0.99 | 5 | the override needs document confidence ≥ 0.90; a split purpose lowers it | WP10 (pooled gates) |
| Residence questions typed UNKNOWN (COUNTRY/LOCATION split) or routed AMBIGUOUS (COPY 0.94 / HUMAN 0.06) | 6 | pooling needs confidence ≥ 0.90; screeners need the COPY_KNOWN label | WP10 + WP2 round 6 |
| Bare First Name / Last Name / Email / LinkedIn held on Jev's hedge (UNCLEAR 0.06–0.08, EXPLICIT 0.23 once) | 6 | strict clarification 0.75–0.89 | WP2 round 6 (deterministic bare contact) |
| Yes/no experience screeners not run | 5 | route AMBIGUOUS at COPY 0.85–0.91 | WP2 round 6 (route mass) |
| Labels lost to placeholders or ids (Ashby "Type here…", "Pick date…"; Breezy `section_…_question_N`; Lever), a radio group labelled by its first option, BambooHR pre-filled custom selects, Teamtailor hidden import input, Jobvite apply | 14 | browser normaliser and runtime | WP1 round 7 |
| Consent and attestation statements (privacy notice, "information is true", contact consent, no AI tools in interviews) | 6 | explicit by design today | WP2 round 7 (reusable statements, strict coverage) |
| One-time answers with null defaults (salary ×6, earliest start ×4, previously employed here ×3, AI tools ×2, county ×2, pronouns, non-compete, government official, familiarity) | 22 | the person's data | simple-answers import; `holds` + `answer --reuse global` (WP9) |
| Budget exhausted on an 8-question Breezy form (4 narratives) | 4 | 48 calls / USD 0.50 per run | WP2 round 6 (budget scales with the form) |
| Jev MALFORMED_RESPONSE (16-option referral multi-select, Gem attestation) | 2 | no retry | WP2 round 6 (one retry) |
| Narratives without grounding facts (2), CAPTCHA (1), RELOCATION questions naming a place (2) | 5 | correct holds; bottleneck 6; residence-first rule | WP2 round 6 for RELOCATION |

Pilot 6's six held applications, rerun after ce7c0ce: Workable and Rippling (GoFish) reach the
final review; the two Greenhouse forms failed only on the Country react-select, whose committed value
Greenhouse renders as two text nodes ("+" and "1") that the display reader joined with a space;
after WP1 round 6 (54a28a6) FirmPilot reaches the final review step and Reunion holds only on
First Name (Jev's source-scope hedge, WP2 round 6); the Rippling state-list question is typed STATE
but routed AMBIGUOUS (WP2 round 6); the other Rippling form needs the person's answers (work
authorization "Permanent / Temporary", desired salary, travel level).

## Evening (September 24): what landed after pilot 7 and what the reruns show

Committed on j-workspace through d1a4f26: WP2 rounds 6–7 (bare contact fields copied without
the source-scope hedge; residence, relocation and experience screeners keyed on route mass; saved
answers matched by semantic type with a type-anchored gate at 0.90/0.85 under a truthful-answer
criterion; reusable consent statements; city lookups type the bare city and retype "City, Region"
only when the site offers nothing; new one-time keys), WP9 batch tooling (`prepare-batch --retry`,
`holds`, grouped `batch-report`), WP10 classifier rounds 1–2 (`full-form-routing-v13`: pooled gates
on pooled mass, option caps and batched requests under the 60 KB bound, required resume approved on
a sure route, Yes/No and address-box pooling, consent demotion), WP11 dashboard fixes, three
independent review passes (all findings fixed or assigned; the typed-newline submission hole and the
trace-content leak are closed).

Prepare-only reruns of the held applications from a clean checkout, after each round:

| form | before | after |
| --- | --- | --- |
| Rippling GoFish (state-list residence) | held: "'TX' is not one of the options" | final review |
| Greenhouse Reunion | held: First Name, Email; then Country readback; then city lookup | final review |
| Greenhouse FirmPilot | fill failed on Country (+ 1 dial code) | final review |
| Ashby Bestow (required resume) | held: purpose not a verified attachment | final review |
| Greenhouse Appspace | 13 holds, whole form lost to the request bound | 6 holds (salary, budget range, agency-experience typed CONSENT, two narratives, platforms multi-select) |
| Greenhouse Vercel | countries question held | answered; work authorization options, privacy consent, attestation remain |
| Rippling (other) | 4 holds | 2 holds: work authorization "Permanent / Temporary", travel level |
| Greenhouse Pomelo | 3 holds | 2 holds: work authorization, sponsorship (see below) |

What the traces say about the remaining holds:
- **Work authorization and sponsorship** hold on most forms even after type-anchored matching, and
  Jev is right to refuse: the saved question "Are you currently authorized to work in the US?" is
  narrower than "authorized to work in the United States for any employer" or "Permanent /
  Temporary". WP2 round 8 replaces wording equivalence for these two types with derivation from one
  stated status key (`work_authorization_status`, closed vocabulary) through a truthful-option
  decision plus a deterministic table.
- **One-time answers** the person still owes: race/ethnicity (a new key), salary period label,
  "how familiar were you with the company", "have you used AI tools", the Ashby free-text work
  authorization wording. `interviewmaxxing holds` lists them with the `answer --reuse global` lines.
- **Browser side** (WP1 round 7, in progress): placeholders and ids as labels (Ashby "Type here…",
  Breezy `section_…_question_N`, radio groups labelled by an option), BambooHR pre-filled
  comboboxes, Teamtailor hidden import input, Jobvite apply, the Ashby location typeahead whose
  suggestion portal trips the fill guard, and the retype regression for multi-line text areas.
- **Provider timeouts**: one Greenhouse form lost six fields to a timed-out routing batch; WP10
  round 3 retries a timed-out batch once, halved.

Pending merges: WP6 (dialog wizards, embedded iframes, JazzHR/Dayforce) integrating WP8 (approve →
authorize → submit exactly the approved packet, mock only); WP7 (Workday up to Review; Playwright
now blocks LinkedIn hosts because Workday embeds an "Apply with LinkedIn" gadget); WP12 (the
person's professional stories chunked into a `story` kind in pgvector, retrieval for cover letters
and narrative answers, Opus 5.5 at high effort, a no-AI-slop rewrite that must re-pass grounding).
LinkedIn Easy Apply is out of scope for good.

**Merged late evening (j-workspace 99485ca).** WP1 rounds 7–9 (labels from the shown question,
BambooHR comboboxes, hidden import inputs, Jobvite, lookup portals and Floating UI's page-wide
aria-hidden marks, referral wording typed on any control); WP6 with WP8 (dialog wizards, embedded
iframes, JazzHR/Dayforce/Jobvite flows; approve → authorize → submit exactly the approved packet,
mock-only, behind `IMX_ALLOW_SUBMISSION=1`, `--yes` and a per-application approval); WP12 rounds
1–2c (stories and the SEO story in pgvector, resume-dated story facts, motivation questions as
cover-letter narratives, 31 derived years-of-experience facts, bounded evidence everywhere, and
story claims that contradict the resume dropped from an answer's evidence rather than holding
it). Full gates on 551717e: browser, core, service, generation and candidate suites plus 25 e2e
tests pass. Pending: WP7 Workday rebase, WP12 round 3 (motivation from alignment, the writer's
output budget under high effort, derived facts as screener evidence, enumerations), review pass 4,
then `prepare-batch --retry` over the 120 held applications.

**Stories in the RAG (WP12, merged a966528).** The person's four professional stories are indexed
as a `story` kind (15 chunks, 39 story-provenance facts merged into the profile); narrative fields
retrieve story chunks with the facts, the writer runs at high effort, and a no-AI-slop rewrite
must re-pass grounding. The first live rerun proved the grounding guard: every story-backed
narrative was held by the independent review because the extraction had stamped all story facts
with the year 2026 while the resume dates the same role Mar 2024 – May 2025. Round 2 makes facts
carry only stated or resume-linked dates and re-indexes; it also writes "what interests you about
us" questions as cover-letter narratives (today skipped as personal preferences) and derives
years-of-experience facts from the resume timeline.

## Bottlenecks to debug next (ordered by jobs affected)

1. **Sign-in-gated backends need the user's Chrome.** LinkedIn Easy Apply (78),
   Wellfound (30), Indeed (10), Workday (51, account creation), iCIMS (8) and the other
   blocked buckets total 122–200 jobs. They need the OpenCLI session, one at a time,
   and OpenCLI cannot attach the resume file itself (Browser Bridge upload is refused),
   so each needs a manual attach step.
2. **Custom controls on the largest backends.** Greenhouse and Lever forms use React
   select widgets for country, work authorization, sponsorship, start date and
   "how did you hear about us". The runtime operates only unambiguous single-select
   ARIA listboxes; anything else stops as `needs_input`. The post-fix pilot confirmed
   it on every Greenhouse and Rippling form reached (phone country picker, location
   typeahead, work authorization, sponsorship, gender, referral source).
3. **Resume upload behind styled buttons.** Greenhouse hides the file input behind
   "Attach / Dropbox / Google Drive / Enter manually"; Lever behind "Attach Resume/CV".
   The runtime uploads only to a visible or labelled `input[type=file]`.
4. **Jev near-threshold holds on trivial fields.** A GitHub URL field held once at a
   0.94 applicant-source score. Profile-URL fields now accept the current-versus-
   historical ambiguity without an extra call; other identity fields still use the
   strict clarification, which can flake at the 0.95 threshold.
5. **Consistency review cost.** Half of the narrative fields escalate to the Opus
   consistency review (USD 0.056 each) because the Jev check holds compatible facts
   with the same key. Tightening the consistency criteria or caching the verdict per
   fact set would cut cost by a third.
6. **Stale inventory.** 2–3% of Saved jobs are already closed by HTTP evidence alone;
   run the batch's `closed` outcomes back into the pipeline before the next sweep.
7. **Dashboard path is single-run and TEST_ONLY.** The service executor runs one
   application at a time and refuses non-loopback URLs. Bulk preparation goes through
   the CLI harness; switching the dashboard to LIVE and queueing runs is future work.
8. **Machine capacity.** Each headless Chromium worker costs 150–300 MB; the
   find-500 run pushed this 24 GB machine into 19 GB of swap with 30 agents. Keep the
   batch at 3–4 workers and measure before raising it.
9. **Submission is still disabled everywhere by design.** Turning it on needs an
   explicit change to the runner, the store restriction and the browser policy, plus
   the CAPTCHA-at-submit user step on Greenhouse, Lever, JazzHR and SmartRecruiters.

## How to run the next batch

```bash
uv run --no-sync python scripts/index_saved_jobs.py --inventory /abs/private/application-urls.json \
  --env-file /abs/env.local --connection-file /abs/private/connection.json --receipt /abs/private/index.json

uv run --no-sync interviewmaxxing prepare-batch --inventory /abs/private/application-urls.json \
  --backends greenhouse,ashby,lever,workable,rippling,jazzhr,bamboohr,breezy,gem \
  --workers 3 --limit 40 --ai-routing --env-file /abs/env.local \
  --writer-model anthropic/claude-opus-5.5 --rag-connection-file /abs/private/connection.json
```

Review prepared applications with `interviewmaxxing status APP`, the evidence under
`$IMX_HOME/artifacts/APP/`, and the dashboard. Nothing is submitted by either command.
