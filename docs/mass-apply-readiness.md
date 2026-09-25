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

**Retry of pilot 7 (22:05, `prepare-batch --retry pilot7-20260924`, pilot branch = head + WP7, with
the person's imported status, race and salary answers).** 32 of the 40 applications were retried
(6 already prepared, 2 need only explicit answers): 3 more prepared, 27 held, 2 failed
(BambooHR conditional reveals). Pilot 7 stands at 9 of 40 prepared, from 1 at the start of the
day. The 118 remaining holds: 46 yes/no or select screeners about specific experience (Amazon
DSP, MMM/MTA, incrementality, ABM, agency environment, platforms managed; WP12 round 3, not yet
merged when this ran, lets screeners use the story and derived facts, the rest are genuine gaps
for the person's stories to cover), 18 narratives (same round: motivation from alignment, output
budgets), 8 salary variants (WP2 round 10, paused: range and period selects deterministic, base
salary from the saved figure), consent and attestation statements (the person's optional consent
keys), earliest start date (the saved value does not fit the sites' option wordings), and one-off
questions (county, pronouns, AI tools, familiarity, location preference).

**September 25, morning: the cloud detour, the recovered rounds, and retries three and four.**
Overnight the WP2, WP10, WP1 and WP9 rounds ran as Claude Code cloud sessions and finished green,
but none could push: the account's GitHub connection never covered this repository, so the CLI
uploaded bundles and the sessions' git proxy refused every push (see `docs/cloud-rounds.md`). The
work came back as patch attachments downloaded from the shared session pages and applied with
`git am`; the review pass 5 report is in `docs/reviews/pass-5-2026-09-24.md`. Everything is merged
at d4539e9 (gates: 5043 passed, 25 e2e). No further work goes to the cloud.

The person filled every reusable key (53), stated his experience (8 years professional, 7 paid
media across Google, Meta and LinkedIn Ads, 6 SEO, 7 performance marketing, 5 leading teams),
confirmed all story facts, and added his LinkedIn entries, the Adscriptly site (30 pages) and a
first-person builder story to the corpus: 131 verified facts and about 165 story passages, from
39 facts and 19 passages the evening before.

Retry three (answers only, head 8775a34): 29 run, 1 more prepared (10 of 40), holds 115 → 106.
Retry four (merged head d4539e9, facts and years in): 27 run, 1 more prepared (11 of 40), 23 held,
3 failed, holds 105 → 95. The salary derivation cleared seven of the eight salary holds. What still
holds, from the traces:

1. *Experience screeners (32 yes/no, 6 multi-select).* Three defects, none about the facts: the
   screener never runs when the route ends AMBIGUOUS (confidence 0.84–0.92), a Jev YES at 0.99
   without `supporting_ids` is discarded as UNKNOWN, and the evidence set is the top 8 retrieved
   facts, which rarely include the `years_experience.*` and user-stated facts. WP2 round 11 has
   these as an addendum; WP10 round 6 types the "N+ years of …" questions.
2. *Conditional follow-ups, Yes/No answers on free-text fields, SMS consent, country lists*: WP2
   round 11.
3. *Paylocity controls and its work-history block*: WP1 round 12.
4. *Consents and one-offs the person answers once*: the answer sheet (`holds --sheet`,
   `answer --sheet`) is merged; the next step is to generate it and have the person fill it.
5. *Genuine gaps*: programmatic/DSP, MMM/MTA, incrementality tests, orthodontics, 500-account
   portfolios. Honest Nos the person gives once through the sheet.

**Fifth merge wave (September 25, 17:30, j-workspace 480fed4).** WP12 round 6 (cover letters graded
on the text that ships: one combined grounding-plus-rubric review per draft, per-requirement fact
retrieval with the long-form stories as the spine, the rubric's line rules checked in code with
corrective rewrites, a per-letter allowance of 24 calls / USD 2.50 on top of the writer field's, the
person's voice register from his 2017 posts and the Adscriptly docs page as style only) and WP2
round 13 (`metro_area`: an Austin-metro job takes on-site or hybrid as the posting states, any other
US job takes Remote, office lists pick the metro office or Remote, "if you are not based in Austin"
relocation questions read the verified address). Gates on the combined tree: 5980 passed, 9 skipped, 3 xfailed in 17.5 minutes, 25 e2e, ruff and mypy clean. The 2Captcha
MCP server is configured in `.mcp.json` (key `TWOCAPTCHA_API_KEY` in `env.local`); the runtime's
own solver is WP1 round 14, still in flight with the questions that appear mid-fill, the Paylocity
leftovers and the consent and attestation controls.

Cover letters after four batches of three (Base Power, Maximus, Superhuman), all written by
`scripts/rag_answers.py draft --cover-letter --application-id`, nothing submitted:

| batch | code | result |
| --- | --- | --- |
| 1 | 776b6f8 | three shipped or held; the read-only judge graded all three **D** (attribution clauses, generic company facts, side projects as proof, bare year ranges) and ranked ten fixes |
| 2 | 4cb0bdb | ten fixes applied; **Maximus READY** (321 words, 15 calls, USD 0.66, rubric PASSED, humanizer REWRITTEN, lint clean); Base Power and Superhuman held (bridging claims; one date range) |
| 3 | 7efb864 | all three held: side projects as full sentences, "in 2024" for work spanning 2024-03 to 2025-05 (the round's own date rule, reverted) |
| 4 | fd7c4a1 | **Base Power READY** (284 words, 15 calls, USD 0.98, rubric PASSED on the shipped text, humanizer REWRITTEN); Maximus and Superhuman held on `Review HTTP_402` |

Spend over the four batches: USD 11.45 in 258 provider calls. The loop stopped when OpenRouter
answered HTTP 402: the account shows USD 0.85 of its 125 credits left, so batch 5, retry eight and
any preparation that writes a narrative wait on a top-up. The judge is grading the two READY letters
(`.imx/rag-writing/cover-letters/batch-4/JUDGE.md`). The worker's own reading of Base Power is B/B+:
every HARD line passes; the soft misses are two stacked scale figures in the proof, the
offline-conversion rebuild told four times, and no tradeoff because the passage states none.

**Retry seven (16:11, fourth wave with the standing policies imported).** 14 run, 2 more prepared:
**22 of 40 prepared**, 9 held, 3 failed; 6 skipped as needing only the person (CAPTCHA, consent
clicks). 13 holds remain: two Greenhouse EEO blocks and a Teamtailor LinkedIn field that appear
mid-fill and are now named but not yet resolved in the same run, Paylocity's address line and
work-history dates, two Greenhouse checkbox groups whose options were never observed, the
data-processing consent and "double-check" attestation controls, the case-study question, two
narratives the corpus does not cover, one experience multi-select. WP1 round 14 takes the runtime
items and adds CAPTCHA solving through the person's 2Captcha account behind a flag and a spend cap;
WP2 round 13 decides work arrangement by the job's metro (Austin area: on-site or hybrid as the
posting requires; elsewhere: remote).

**Fourth merge wave (16:10, j-workspace fbcd23f).** WP11 round 3 (the dashboard review-and-submit
lane: a Prepared queue, every answer with its provenance badge and RAG citations, approve and the
gated submit from the page, edit and re-prepare), WP1 round 13 (forms that re-render mid-fill:
renamed questions matched by shape and position, inserted follow-ups taken in, appearing questions
named), WP9 round 4 (mass-run tooling: sheets per batch, per-entry sheet results, `--only-app`,
yield over retries, `--exclude-batches`) and WP2 round 12 (the person's standing answer policies,
one Jev class decision per unsettled required question: experience claims Yes, thresholds Yes,
certifications Yes, current-or-former-employee No, sanctioned locations No). Gates on the
combined tree: 5860 passed, 25 e2e. The policies are imported; retry seven runs on this head.

**Retry six (14:45, second merge wave: WP2 round 11, WP1 round 12, WP12 round 5, WP10 round 6, WP8
round 3).** 18 run, 4 more prepared: **20 of 40 prepared**, 10 held, 4 failed, holds 24 → 15. Of the
40, six are skipped because their only open items need the person (a CAPTCHA at the final step, a
consent the runtime never operates). The 15 remaining holds: two identity fields on CAPTCHA-blocked
forms, the Paylocity work-history dates and address line, two Greenhouse checkbox groups whose
options were never observed, the case-study question (its data sits outside the recorded field),
two narratives the corpus does not cover (programmatic campaign, service-line P&L), one experience
multi-select, one screener wording the standing policies will take. The four failures are all
re-rendering forms: BambooHR's Fabric text fields change ids when a value is set (two forms),
Greenhouse's EEO block appears after the custom questions (KnowBe4), and a Teamtailor text field
appears mid-fill. WP1 round 13 takes them.

**Retry five (13:05, after the answer sheet).** The person declined to answer 84 open questions
one by one and stated standing rules instead: any "do you have / have you" experience question is
Yes (he vets every application before it enters the batch), every experience threshold up to his
stated years is Yes, every certification of truth is affirmative, every current-or-former-employee
question is No. The lead applied them semantically over the sheet (70 answers, 19 applications,
saved globally) and left 14 for the person. Result: 26 run, 3 more prepared (14 of 40), 16 held,
7 failed, holds 95 → 30. What remains: 7 salary fields (the derivation cannot parse the saved
figure's format; WP2 round 11 addendum), Paylocity's controls and work-history block (WP1 round
12), 4 narratives, and 7 fill failures: Lever's resume uploader still uploading when the wait ends
(3), conditional reveals the round-11 path did not catch on a Greenhouse form and two BambooHR
forms (3), a Greenhouse checkbox-group click timeout and a phone read-back formatting mismatch
(WP1 round 12 addendum). WP2 round 12 turns the standing rules into `answer_policies` that Jev
applies to each new question.

**Second retry on the merged head (22:29, `prepare-batch --retry pilot7-20260924 --all --batch-id
pilot7-retry2`, j-workspace 9ca484b = WP2 round 9 + WP12 rounds 3–4 + WP7 + WP1 round 10).** 29 of the
40 applications were run again (9 prepared, 2 need only explicit answers): 0 more prepared, 27 held,
2 failed (the same BambooHR conditional reveals; WP1 round 11 is on them). Holds went 118 → 115 at
USD 7.80 of provider cost over 1,846 calls (median 15 s per application). Read from the projected
`routing.trace` events, the holds fall into six causes, none of them the widget work of the day:

1. *The person's empty keys* (about 20 holds). Thirteen simple-answer keys are still null: pronouns,
   county, disability status, family government official, non-compete, AI-tools use, familiarity with
   the company, `career_motivation`, and the five consent/attestation keys. They map one-to-one onto
   the pronouns, county, "have you used AI tools", "how familiar were you with Upstart", government
   official (three fields), non-compete, SMS/privacy/AI-policy consents and "I certify" attestations.
2. *Motivation narratives* (four "why us / why you're a good fit / what interests you" fields). WP12
   round 4 requires a cited story passage or the `career_motivation` statement before writing; the
   statement is null and the stories do not mention these employers, so the fields hold before any
   writer call (trace `motivation_narrative/MOTIVATION_PURPOSE`, no draft). Writing the statement once
   unlocks them.
3. *Experience screeners* (34 yes/no, 6 multi-selects, 4 numeric). Story facts are UNVERIFIED until
   the person trims and imports the two `.confirm.json` files, so the screeners see resume facts only:
   the platform multi-selects end `fact_screener/UNDECIDED` (SUPPORTED 0.97–1.0 but no option source),
   the numeric ones `UNKNOWN`. Several are genuine gaps the stories do not cover (Amazon DSP, MMM/MTA,
   incrementality tests, orthodontics, 500-account portfolios); absence of a fact is never a "No", so
   those need the person's answer once, globally.
4. *Saved answers that exist but do not reach the field* (13 holds, all WP2 round 10 follow-up, sent
   to the cloud session): eight salary wordings (base, target, monthly, hourly, annual, total, "be as
   specific as possible", range minimum) fail wording equivalence (NONE or BELOW_GATE 0.59–0.67) now
   that round 9 took SALARY_EXPECTATION out of the type-anchored gate → derive from the saved figure
   with period conversion; "Earliest Start Date?" (Lever select, four applications) ends
   `VALUE_DOES_NOT_FIT` 0.87–0.92 then option NONE → bucket the saved date onto the option ranges;
   "Can you work legally in the United States?" (JazzHR, five options) flipped between Jev 0.98 and
   0.91 on the status derivation → deterministic plain Yes/No within larger option sets and a repeat
   decision near the gate; "Location Preference" and "fully in-person in Austin" → a
   `work_arrangement_preference` key.
5. *Classifier misses* (seven wordings, sent to the WP10 round 5 cloud session): "Are you authorized
   to be employed in the United States?" untyped (so no status derivation), "Do you have experience
   working at a digital marketing agency?" typed CONSENT, "largest overall annual ad spend" typed
   WEBSITE, the time-zone multi-select untyped although `available_time_zones` is saved, County
   UNKNOWN, the Paylocity "Company name" untyped, "How well do you know us" UNKNOWN.
6. *Unsupported controls* (nine holds): Paylocity's address block (Country, State, Address Line 1,
   County) on two applications, two Greenhouse checkbox groups whose options were not observed, one
   "double-check the information above" attestation, one CAPTCHA. Candidate WP1 round 12 (Paylocity).

Process bottleneck found: 107 distinct open questions across the 27 held applications, most of them
legitimately personal, and `holds` answers them one command each. A new cloud round (WP9 round 3)
builds an answer sheet: `holds --sheet FILE` writes every open question with its options and any
below-gate proposal marked as unconfirmed, the person fills the answers in one sitting,
`answer --sheet FILE` saves them with `--reuse global`, then `prepare-batch --retry`.

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

## Bottlenecks to debug next (ordered by holds affected, after the second retry)

1. **The person's inputs.** Thirteen empty simple-answer keys, the `career_motivation`
   statement and the two story `.confirm.json` files cover roughly half of the 115 holds
   (one-off consents and attestations, pronouns, county, AI-tools use, familiarity, the
   motivation narratives, and every screener the stories support). The rest of the
   personal questions need one global answer each; WP9 round 3 (cloud) builds the answer
   sheet so that is one sitting rather than 107 commands.
2. **Saved answers that do not reach typed fields.** Salary wordings (eight variants),
   the Lever start-date select, the five-option legal select and the work-arrangement
   questions all have an answer in the profile and still hold on wording equivalence.
   WP2 round 10 (cloud) derives them from the saved values the way the work-authorization
   status is derived. *Status, September 25:* landed through WP2 round 13 (salary shapes,
   start-date buckets, the legal select, the standing answer policies, the metro rule for
   work arrangement); what still holds here is a wording the policies do not cover.
3. **Classifier misses.** Seven live wordings were untyped or mistyped (an authorization
   question with no type, an agency-experience question typed CONSENT, an ad-spend
   question typed WEBSITE, the time-zone multi-select untyped). WP10 round 5 (cloud) adds
   them to the v13 gates.
4. **Controls the runtime still cannot operate.** BambooHR conditional reveals fail two
   applications (WP1 round 11, cloud); Paylocity's address block (country, state, address
   line, county) and two Greenhouse checkbox groups whose options were never observed
   hold four more (candidate WP1 round 12). *Status, September 25:* WP1 rounds 11–13 landed
   (conditional reveals, Paylocity's controls, forms that re-render mid-fill); round 14 is
   in flight with the questions that appear during the fill, the Paylocity leftovers, the
   checkbox-group options, the consent and attestation controls and the 2Captcha solver.
5. **Sign-in-gated backends.** LinkedIn Easy Apply is out of scope (the person applies
   by hand). Wellfound (30), Indeed (10), Workday (51; the account step is left to the
   person, WP7) and iCIMS (8) still need the person's browser session or an account.
6. **Submission stays disabled by design.** WP8's approve → authorize → submit path is
   mock-only; the reviewer's rule stands: no `submit-approved` on a real employer before
   the cloud rounds land and a review pass covers 551717e..HEAD.
7. **Cost and capacity.** A 29-application retry costs about USD 7.80 in provider calls
   (median 15 s per application) at four headless workers; narratives at high effort
   add USD 0.10–0.30 per field. Keep batches at four workers on this machine.
8. **Stale inventory and the dashboard.** 2–3% of Saved jobs are already closed
   (`prepare-batch` moves their cards to Closed); the dashboard executor is still
   single-run and loopback-only, so bulk preparation goes through the CLI.
9. **Provider credits are a hard stop.** Every Jev decision, writer call and review goes
   through the OpenRouter account in `env.local`; when it ran dry on September 25 the
   letter loop stopped with HTTP 402 and every narrative field would hold the same way.
   Check the balance (`GET /api/v1/credits`) before a batch; the full inventory with one
   letter per form is USD 200–360 at the measured rates.

## How to run the next batch

Worker rounds that run as Claude Code cloud sessions: see `docs/cloud-rounds.md` (start, steer,
and the GitHub push requirement).

```bash
# fresh inventory
uv run --no-sync interviewmaxxing prepare-batch --inventory /abs/private/application-urls.json \
  --backends greenhouse,ashby,lever,workable,rippling,jazzhr,bamboohr,breezy,gem \
  --workers 4 --per-job-timeout 600 --limit 40 --ai-routing --env-file /abs/env.local \
  --writer-model anthropic/claude-opus-5.5 --rag-connection-file /abs/private/connection.json

# after answers or fixes: every held and failed application of a batch again
uv run --no-sync interviewmaxxing prepare-batch --retry BATCH_ID --all --batch-id BATCH_ID-retryN \
  --workers 4 --per-job-timeout 600 --retry-retryable 1 --ai-routing --env-file /abs/env.local \
  --writer-model anthropic/claude-opus-5.5 --rag-connection-file /abs/private/connection.json
```

A batch that predates recorded run options (pilot 7) does not carry its worker count into a
retry, so pass `--workers` explicitly. Read the result with `batch-report BATCH_ID`, the open
questions with `holds`, and one application with `status APP` or `events APP --verbose`.
Nothing is submitted by any of these commands.
