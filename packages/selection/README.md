# interviewmaxxing-selection

Jev job selection (J2). This package makes an auditable APPLY/SKIP/REVIEW decision for one
observed `JobListing`, using the user's `SelectionPreferences` and the candidate's
verified qualifications. The result is a core D0 `JobSelection`.

The package never opens a browser, contacts an employer or submits anything. An
effective APPLY only makes a listing eligible for the existing requested-application
flow (`record_request(candidate_id, listing.application_url or listing.source_url)`).

## Provider

Jev runs on the OpenRouter Decisions API: `POST https://openrouter.ai/api/alpha/decisions`.

- It is never called through `/chat/completions`, and the client rejects a chat URL.
- It is never rejected for missing from `/api/v1/models`; that catalogue omits decision models.
- The requested model `typesafe/jev-1.13` is pinned (`DEFAULT_JEV_MODEL`). Each call's returned model, for example `typesafe/jev-1.13-20260917`, is recorded.

Request shape: `{model, state, questions: {name: {type: "choice", instructions, criteria: {option: meaning}}}}`.

Response shape, as observed live on 2026-09-22: `{model, answers: {name: {type, choice, probabilities, confidence}}, usage: {input_tokens, output_tokens, cost}, id, provider}`. Published examples use camelCase usage; the client accepts both casings.

### Response validation

A response is rejected as `MALFORMED_RESPONSE` in any of these cases:

- a requested question is missing, or an unrequested answer is present;
- an answer has the wrong type, or its choice is not an offered option;
- the probabilities do not cover exactly the offered options;
- any number is outside [0, 1] or not finite;
- the probabilities do not sum to 1 within 0.01;
- the chosen option is not the most probable one.

### Retries and errors

- **Timeouts and retries.** The request timeout is explicit (default 20 s). Timeouts, network errors, 408 and 5xx responses are retried with bounded backoff. The default is 3 attempts; the maximum is 5.
- **Rate limits.** A 429 is retried only when `Retry-After` is 10 s or less. Otherwise it is returned as `RATE_LIMITED`.
- **Errors returned immediately.** 401, 402 and 403 are not retried. They become `UNAUTHORIZED`, `PAYMENT_REQUIRED` and `FORBIDDEN`, each with an `action` hint.
- **Redaction.** Error text is redacted and never contains the key.

## Credentials

`load_api_key(env_file=None)` reads `OPENROUTER_API_KEY` from the first source that is set:

1. an explicit file;
2. the file named by `IMX_OPENROUTER_ENV_FILE`;
3. the process environment.

Only that one line of the env file is parsed. `ApiKey` has redacted `repr`/`str`, cannot be pickled, and exposes the value only through `reveal()`. The key never appears in arguments, logs, storage or receipts.

## Selection

```python
from interviewmaxxing_core import JobListing, SelectionPreferences
from interviewmaxxing_selection import (
    CandidateEvidence,
    JevClient,
    SelectionService,
    SelectionStore,
    load_api_key,
)

service = SelectionService(
    client=JevClient(load_api_key()),  # None -> REVIEW with NOT_CONFIGURED
    store=SelectionStore(),  # $IMX_HOME/selection/selection.sqlite3 (0600)
    application_lookup=lambda listing: existing_app_id_or_none(listing),
)
outcome = service.select(listing, SelectionPreferences(), CandidateEvidence.from_profile(profile))
outcome.selection  # core JobSelection (effective_choice, model_decision, holds, usage, hashes)
service.is_current(
    outcome.selection, listing, prefs, candidate
)  # False after any input/pref/model/threshold change
```

`candidate=None` (no profile yet) is allowed when `candidate_id` is supplied to the service or call. The decision is then held with `MISSING_PROFILE` and can never be an effective APPLY.

### Steps

1. **Code checks.** Code compares stated pay with the floor using core `meets_floor`. It never estimates pay: hourly or weekly pay, another currency, or only a lower bound under the floor count as NONCOMPARABLE. Missing pay is UNKNOWN. Both are kept by default (`unknown_compensation=KEEP`). Code also checks the work arrangement and location; commute is never considered. Remote eligibility is a US-wide region, separate from the Austin onsite/hybrid target.
2. **Hard constraints.** The following mean SKIP without calling Jev:
   - the listing is CLOSED;
   - an application already exists (`application_lookup`);
   - the top of the stated pay range is below the floor;
   - the role is onsite or hybrid outside the accepted locations;
   - the role is remote but remote work is disabled;
   - the title contains an excluded keyword;
   - the company is excluded.
3. **Cache.** An identical earlier Jev decision for the same candidate is reused. The cache key covers the candidate ID, model, the rubric/threshold version, `job_evidence_hash`, `candidate_evidence_hash` and `preferences.fingerprint`. Provider failures are never cached.
4. **Jev calls.** Two calls are made:
   - **Focused questions:** `role_match`, `seniority_match`, `qualification_match`, `location_eligibility`, `preference_match` and `listing_consistency`.
   - **Final question:** `selection` (APPLY/SKIP/REVIEW), given those assessments and the statuses computed by code.

   Listing text appears only in `state.listing` as untrusted data. Instructions and options are rubric constants.
5. **Holds.** Any hold turns APPLY into REVIEW. Holds for contradiction, low confidence, suspected injection and provider errors also turn SKIP into REVIEW, so promising roles are not silently discarded. The holds are:
   - missing profile;
   - missing or partial description;
   - unknown location;
   - ambiguous eligibility;
   - insufficient or contradictory focused answers;
   - final confidence below `SelectionPolicy` (default 0.8 for both APPLY and SKIP);
   - suspected instruction injection;
   - provider failure.

Each fine-grained `HoldReason` is recorded as a core `PolicyHold`, with the reason name as the prefix of its detail.

`SelectionOutcome` adds audit detail the contract has no place for: focused assessments, per-call model IDs, generation IDs, the detailed provider failure, the pay and location statuses, the location tier, and the cache key. The store also keeps the exact job and candidate evidence snapshots, the preferences, the requests sent and the raw responses.

### Location priority

`SelectionPreferences.location_priority` (core `LocationPriority`) is an ordinal preference. It is never a weight and never an exclusion. The default, `STRONGLY_PREFER_ONSITE_HYBRID`, is the user's stated preference: matching Austin onsite/hybrid roles rank well above US-wide remote roles, and remote roles stay eligible.

- **Tier.** `location_tier(location_status, priority)` gives `PREFERRED`, `SECONDARY`, `EQUAL` (for `BALANCED`) or `UNRANKED`. A missing or unaccepted location is `UNRANKED` and never counts as Austin.
- **Jev.** The state carries `preferences.location_priority`, `checks.location_tier` and a plain `checks.location_priority_reason`. The final question states the relative importance:
  - a preferred-tier role with solid role and seniority fit favors APPLY or REVIEW even when qualifications are partial;
  - a secondary-tier remote role needs clearly strong fit for APPLY, otherwise REVIEW;
  - location priority is never by itself a reason to SKIP.

  If Jev skips an eligible remote role despite strong focused answers, the decision becomes REVIEW (contradictory evidence).
- **Record.** `selection.reasons` starts with the ranking reason, and `SelectionOutcome.location_tier` stores the tier.
- **Cache.** The priority is part of `preferences.fingerprint`, so changing it invalidates earlier decisions. The rubric version is `jev-selection-rubric/2026-09-22.6`.
- **Ranking.** `rank_outcomes(outcomes)` orders results as follows:
  1. every SKIP last;
  2. then by tier, with PREFERRED and EQUAL first, then SECONDARY, then UNRANKED;
  3. then APPLY before REVIEW;
  4. then Jev's APPLY probability.

  An Austin REVIEW therefore ranks above a remote APPLY. `ranking_reason(outcome)` explains the position.

### Role focus (semantic, not title matching)

`SelectionPreferences.role_focus` describes the target work. The default is the user's own description: a performance marketing operator doing paid acquisition, paid media, growth, demand generation and digital marketing leadership. `target_titles` (for example Paid Media Manager, Performance Marketing Manager, Growth Marketing Manager, Demand Generation Manager and Digital Marketing Manager) are search seeds, never an exact-title allowlist. No default title exclusions exist.

- **Jev state.** Jev receives `preferences.role_focus` and `preferences.representative_titles`.
- **`role_match`.** This question judges actual duties and ownership:
  - paid acquisition budget or channel ownership;
  - experimentation;
  - measurement and attribution;
  - funnel, pipeline or revenue outcomes;
  - team or channel leadership.

  A differently titled role with those duties is a match (for example Acquisition Lead). A similar-sounding title with other duties is not. Title keywords are never proof. Pure data, software or platform engineering is a mismatch. Technical tools inside marketing work (APIs, SQL, automation), whether in the listing or in the candidate's verified facts, are not held against a marketing fit.
- **Holds.** Jev APPLY with `role_match=adjacent` becomes REVIEW (`ROLE_FOCUS_UNCONFIRMED`). APPLY with `mismatch` becomes REVIEW (contradictory evidence).
- **Code.** No code path gates on titles; only the user's explicit `excluded_keywords` do.
- **Reasons and ranking.** `selection.reasons` includes a role-focus reason. `rank_outcomes` orders duty match before adjacent within the same tier and decision, and `ranking_reason` says so.
- **Cache.** `role_focus` is part of `preferences.fingerprint`, so editing it invalidates cached decisions.

### What is sent to Jev

- **Listing:** title, company, location text, work arrangement, stated remote eligibility, pay as raw text, and the description (at most 12,000 characters).
- **Candidate:** verified facts, plus experience and education entries backed by verified facts. Contact details, identity, protected attributes, pay history, employer names, saved answers and generated answers are excluded.
- **Preferences:** role focus, representative titles, onsite targets, remote region, location priority, excluded keywords and notes.
- **Code results:** the pay status, location status, location tier and ranking reason, and hold names.
- **Candidate identity:** never sent. `CandidateEvidence.from_profile` drops contact-bearing values and excluded identity/employer/pay-history/protected-attribute fact keys. Known first, last, full and preferred names, employers (including names from employer facts), and institutions are replaced inside free-text keys/values with neutral placeholders, preserving qualification claims. Education text and experience titles/dates are scrubbed too. `CandidateEvidence` itself rejects recognizable email, phone and URL values or keys; callers constructing it directly must supply already minimized evidence. This is a projection of known profile identifiers, not general-purpose anonymization of arbitrary prose.

URLs, IDs and numeric pay bounds are never sent.

## Live smoke (opt-in, spends credits)

```bash
python -m interviewmaxxing_selection.smoke --live --env-file /path/to/ignored/env.local
```

The smoke runs 5 fictional selections with 10 calls, costing about USD 0.0007. The receipt includes each location tier and the ranked order.

Add `--semantic` to run 3 role-focus cases for a fictional performance marketer instead: an Acquisition Lead, a pure Senior Data Platform Engineer, and a Marketing Manager with events-only duties. That is 6 calls, about USD 0.0005. Offline tests never touch the network.

## API for S2 (local service)

```python
from interviewmaxxing_core import JobListing, SelectionPreferences, JobSelection, LocationPriority
from interviewmaxxing_selection import (
    CandidateEvidence,
    CredentialError,
    JevClient,
    SelectionOutcome,
    SelectionService,
    SelectionStore,
    load_api_key,
    rank_outcomes,
    ranking_reason,
)

try:
    client = JevClient(load_api_key())  # IMX_OPENROUTER_ENV_FILE -> ignored env.local
except CredentialError:
    client = None  # every selection becomes REVIEW with provider_error.code == "NOT_CONFIGURED"
service = SelectionService(
    client=client,
    store=SelectionStore(),  # private; default $IMX_HOME/selection/selection.sqlite3
    application_lookup=existing_application_id,  # (JobListing) -> str | None, from the ApplicationStore
    candidate_id=paths.candidate_id,  # used when there is no candidate evidence
)
candidate = CandidateEvidence.from_profile(profile) if profile else None
outcome: SelectionOutcome = service.select(
    listing, preferences, candidate
)  # sync; do it off the event loop
outcome.selection  # core JobSelection to return/persist
outcome.location_tier, outcome.assessments, outcome.holds  # display detail
service.is_current(outcome.selection, listing, preferences, candidate)  # stale-badge check
ordered = rank_outcomes(outcomes)
[ranking_reason(o) for o in ordered]
store = service.store
store.latest(listing.id, candidate_id=paths.candidate_id)
store.latest_many([listing.id, another_listing.id], candidate_id=paths.candidate_id)
store.history(listing.id, candidate_id=paths.candidate_id)
store.get(selection_id, candidate_id=paths.candidate_id)
store.audit(selection_id, candidate_id=paths.candidate_id)  # snapshots and raw provider exchange; local only, not for the frontend
```

- **Candidate scope.** `select`, `cache_key` and `is_current` accept an optional keyword `candidate_id`. Evidence supplies its own ID; an explicit ID must agree. Without evidence, the call ID overrides the service default; no ID raises `ValueError`. `is_current` rejects another candidate's decision even when qualifications match. All store retrieval methods require candidate scope; `find_cached(listing_id, candidate_id, cache_key)` also scopes its SQL. Legacy rows migrate their owner from the persisted core selection record. The rubric bump invalidates decisions created before these policy/projection changes; custom threshold fingerprints retain full float precision.
- **Batch reads.** `latest_many(listing_ids: Iterable[str], *, candidate_id: str) -> dict[str, SelectionOutcome]` returns found listing IDs only. Empty input returns `{}`. It deduplicates inputs and issues one indexed latest-row query per 400 IDs. `latest` loads one row; both break timestamp ties by insertion order. `history` remains oldest-first.
- **Location uncertainty.** State/country-only onsite text such as `Texas, United States` is UNKNOWN/REVIEW; `Austin TX` parses on either side, and `Austin, MN` does not match Austin, Texas. Canada-only remote eligibility cannot produce APPLY, even if Jev says eligible; Texas-only or unrecognized eligibility is held for REVIEW and never treated as US-wide. Missing remote eligibility still uses Jev's focused eligibility assessment from the observed description.
- **Injection holds.** Direct select/choose/rate decision commands and text addressed to an AI reviewer trigger a hold, while normal instructions such as `Select 'Apply' to start the application` remain ordinary job text. Listing text stays exclusively in untrusted state; detection is a conservative supplementary heuristic, not a guarantee against every possible prompt attack.
- **Threads.** `select` blocks for about 0.5–2 s when it calls Jev, and returns immediately for hard-constraint SKIPs and cache hits. `SelectionStore` holds one SQLite connection, so give each worker thread its own store.
- **Preferences.** Preferences come from the user, including `location_priority`, and persist unchanged. Never return `store.audit` or the API key to the frontend.

## Contract requests to core (additive, optional)

1. **More hold codes.** Add `HoldCode.CONTRADICTORY_EVIDENCE`, `AMBIGUOUS_ELIGIBILITY` and `SUSPECTED_INJECTION`. Until then these are recorded as `INSUFFICIENT_EVIDENCE`, with the precise reason as the detail prefix.
2. **Residence.** Add `SelectionPreferences.residence: str | None`, stated by the user. Remote roles limited to specific states cannot be judged without it and are held as ambiguous.
3. **Keywords.** Add `SelectionPreferences.keywords: list[str]`, the positive custom keywords already present on `JobSearchQuery`. At present only `notes` carries positive preferences into selection.
4. **Probability tolerance.** Consider relaxing the `ModelDecision` probability-sum tolerance from 0.001 to 0.01, or confirm the current choice. Live confidences were two-decimal values, so rounded probabilities are plausible. The service renormalizes a distribution that is off by 0.01 or less, notes this in `reasons`, and keeps the raw answer in the audit.
5. **Dependencies.** None beyond `interviewmaxxing-core` and `pydantic`. HTTP uses the standard library `urllib`.
