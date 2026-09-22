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

`candidate=None` (no profile yet) is allowed. The decision is then held with `MISSING_PROFILE` and can never be an effective APPLY.

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
3. **Cache.** An identical earlier Jev decision is reused. The cache key covers the model, the rubric/threshold version, `job_evidence_hash`, `candidate_evidence_hash` and `preferences.fingerprint`. Provider failures are never cached.
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

`SelectionOutcome` adds audit detail the contract has no place for: focused assessments, per-call model IDs, generation IDs, the detailed provider failure, the pay and location statuses, and the cache key. The store also keeps the exact job and candidate evidence snapshots, the preferences, the requests sent and the raw responses.

### What is sent to Jev

- **Listing:** title, company, location text, work arrangement, stated remote eligibility, pay as raw text, and the description (at most 12,000 characters).
- **Candidate:** verified facts, plus experience and education entries backed by verified facts. Contact details, identity, protected attributes, pay history, employer names, saved answers and generated answers are excluded.
- **Preferences:** titles, onsite targets, remote region, excluded keywords and notes.
- **Code results:** the pay status, location status and hold names.

URLs, IDs and numeric pay bounds are never sent.

## Live smoke (opt-in, spends credits)

```bash
python -m interviewmaxxing_selection.smoke --live --env-file /path/to/ignored/env.local
```

The smoke runs 4 fictional selections with 8 calls, costing about USD 0.0005. Offline tests never touch the network.

## Contract requests to core (additive, optional)

1. **More hold codes.** Add `HoldCode.CONTRADICTORY_EVIDENCE`, `AMBIGUOUS_ELIGIBILITY` and `SUSPECTED_INJECTION`. Until then these are recorded as `INSUFFICIENT_EVIDENCE`, with the precise reason as the detail prefix.
2. **Residence.** Add `SelectionPreferences.residence: str | None`, stated by the user. Remote roles limited to specific states cannot be judged without it and are held as ambiguous.
3. **Keywords.** Add `SelectionPreferences.keywords: list[str]`, the positive custom keywords already present on `JobSearchQuery`. At present only `notes` carries positive preferences into selection.
4. **Probability tolerance.** Consider relaxing the `ModelDecision` probability-sum tolerance from 0.001 to 0.01, or confirm the current choice. Live confidences were two-decimal values, so rounded probabilities are plausible. The service renormalizes a distribution that is off by 0.01 or less, notes this in `reasons`, and keeps the raw answer in the audit.
5. **Dependencies.** None beyond `interviewmaxxing-core` and `pydantic`. HTTP uses the standard library `urllib`.
