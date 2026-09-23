## Call strategies over 2000 fictional listings (stub judge; mechanics only)

| strategy | reached Jev | calls | attempted | input tokens | USD | mean s | p95 s | deferred | APPLY agreement |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| baseline_two_call | 1,369 | 2,738 | 2,793 | 4,977,896 | 0.209 | 1.109 | 1.802 | 0 | None |
| gate_then_two_call | 124 | 248 | 251 | 684,534 | 0.029 | 1.067 | 1.743 | 1,245 | 1 |
| gate_compact_final | 124 | 248 | 251 | 549,238 | 0.023 | 1.067 | 1.743 | 1,245 | 1 |
| gate_combined_selective | 124 | 186 | 189 | 599,058 | 0.025 | 0.798 | 1.5 | 1,245 | 0.927 |
| gate_single_request | 124 | 124 | 125 | 452,376 | 0.019 | 0.513 | 0.923 | 1,245 | 0.944 |

## Cache and invalidation scenarios

```json
{
  "cached_decisions": 124,
  "reobservation_posted_text_only": {
    "cache_hits": 124,
    "of": 124,
    "note": "posted_text is not in job_evidence; hit under current rule"
  },
  "enrichment_adds_application_url": {
    "affected": 95,
    "misses_current_rule": 95,
    "hits_proposed_rule": 95,
    "note": "semantic cache hit only: execution URL/version and duplicate checks must rerun"
  },
  "preference_edit": {
    "invalidated": 124,
    "redecide_budget_calls": 400,
    "listings_redecided_within_budget": 66,
    "apply_covered": "12/12",
    "review_covered": "54/83",
    "note": "reserve max_attempts for each of two calls before scheduling; prioritized APPLY/REVIEW then tier"
  },
  "candidate_fact_version_bump": {
    "invalidated": 124,
    "note": "candidate_evidence_hash changes with any verified-fact edit"
  },
  "rng_check": 0.6394267984578837
}
```
