# C1 review follow-up before downstream contracts are approved

Read-only review on 2026-09-22 against the emerging C1 implementation. The reviewer made no file/database changes. Recheck against the final C1 result; fix any issue that remains and add regression checks. These are within the core owner's existing scope.

1. **Packet/form validation:** `ApplicationPacket.problems_against` accepts a packet for a different form URL/step and accepts an answer whose claimed semantic type differs from the actual inspected field. A `CUSTOM_BOOLEAN` / `CANDIDATE_FACT` boolean answer passed against an actual `ATTESTATION` checkbox because the provenance guard looked only at the answer's self-declared semantic type. Enforce current form identity, semantic compatibility, and sensitive-field provenance using the actual field.
2. **Multistep answer identity:** `UserInput` has no form/step identity and `ApplicationStore.get_user_inputs` retains only the latest input per `field_id`. Two steps may both name unrelated questions `question_0`; the second answer replaces the first on resume. Define a stable question/form scope, persist it, and require correct scope on retrieval/resolution. Include changed-question protection when a field ID is reused. Update interfaces, fixtures and docs consistently.
3. **Choice validity:** `answer_problems` accepts disabled options, a required select's disabled empty placeholder, and a contradictory `value="US", label="Canada"` when the actual option is United States. Validate enabled state and label/value agreement for single and multiple choices, including empty required selections and repeated multi-choice values.

Two contract requirements must also be explicit before C2/C3:

4. **Saved-answer scope:** support an explicit job-specific saved answer as well as explicitly global reuse. The current `SavedAnswer` documents only cross-application reuse. Keep application-local answers local by default; scope matching must not silently promote an answer about one employer into an answer about another.
5. **Verified facts:** distinguish factual confirmation from a numeric confidence default. Add explicit verification metadata, or a precise enforceable verified-only loading contract, so C2 and C3 can reject/omit unverified facts without treating `confidence=1` as proof. Document what the user supplies and what the loader verifies.

Once corrected, run the core verification command and return the exact commit. The user activated `dashboard` while C1 was running; the frontend is building under `apps/web/**` against a service boundary. It will use the same local Python executor and state, with no separate JS application state machine. This does not require C1 to build the frontend; CLI integration I1 and frontend bridge F2 follow.
