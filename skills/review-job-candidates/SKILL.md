---
name: review-job-candidates
description: Review staged Interviewmaxxing jobs for semantic role fit, work location, attendance, salary evidence, availability, and duplicates before saving. Use to approve, hold, or exclude candidate batches, especially misleading titles, uncertain pay, employer contradictions, and cross-source reposts. Does not acquire or apply to jobs.
---

# Review job candidates

Produce explicit, evidence-based decisions for the coordinator. Read the run's actual scope and current duplicate index first. Use [decision rules](references/decisions.md) and the coordinator's [reviewed input contract](../find-jobs/references/helper-commands.md#reviewed-input-contract). Generate the executable schema with `find_jobs.py schema` when constructing an envelope.

For every candidate, inspect the full available evidence and record:

1. **Identity:** actual employer, source posting ID, job-specific URL, employer requisition when proven, and known source aliases. Hold likely duplicates against any existing pipeline lane and other workers' delivered-but-unsaved batches. Use the coordinator's compact staged-identity/claim list to locate overlaps, then compare the named evidence; an index-only checker cannot see all pending work. A generic apply URL or matching company/title alone is not a canonical merge key.
2. **Role:** a short rationale connecting actual duties to the user's role focus. Judge ownership of the work, not title keywords or aspirational company language. If supplied evidence cannot settle the role, HOLD and request specific missing evidence.
3. **Location and attendance:** the actual work location and explicit onsite/hybrid/remote evidence. Resolve card-versus-description conflicts using the primary employer. Do not treat an Austin company address as evidence that this role works there.
4. **Availability:** OPEN, CLOSED, or UNKNOWN with a timestamped source observation. Exclude known-closed/expired roles unless a separately verified active employer posting establishes current availability and identity. Unknown may be saved only under the run's review policy.
5. **Compensation:** employer, estimated, or unknown basis; verbatim wording; numeric bounds only for explicitly stated employer pay with currency and period. Apply the selected pay policy and preserve all caveats.
6. **Completeness and qualifications:** FULL/PARTIAL/NONE description truthfully, plus material experience or seniority concerns. A partial description can qualify only when available evidence establishes fit and attendance; otherwise HOLD. Unknown qualifications are not an invented match.

Use SAVE only when the mandatory scope is supported, no known duplicate exists, and remaining uncertainty is explicitly permitted by the run. Use HOLD when missing evidence or identity must be settled. Use EXCLUDE for demonstrated nonfit, disallowed attendance/location, known closure, or pay below the rule. Do not create pipeline cards for HOLD/EXCLUDE.

Return a native reviewed envelope with the run ID, reviewer ID, full listings, decision, `compensation_basis`, concrete `fit_rationale`, the six evidence fields, `review_reasons`, and `duplicate_of` when applicable. Cite source URLs/observations in evidence. Never pass through a collector's `eligible` boolean as your decision. Do not change a listing's factual evidence merely to make the helper accept it.

Keep review independent from saving: write only assigned review artifacts and a compact summary of decisions, missing evidence, and counts. The coordinator validates, canonicalizes, and saves. If a configured fast model is used for triage, pass only the required job evidence and scope, keep its result in the same contract, and recheck borderline decisions; model confidence is not source evidence or permission to apply.
