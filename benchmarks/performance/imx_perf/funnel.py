"""Closed-form capacity model: what each daily target demands per stage.

Every output is ``modeled`` from the assumptions unless the assumption itself is
``observed``. Nothing here claims achievable throughput.
"""

from __future__ import annotations

from typing import Any

from .config import Assumptions

SECONDS_PER_DAY = 86_400.0


def run_plan(a: Assumptions) -> dict[str, Any]:
    """Page operations and enriched listings for one full search run (observed structure)."""
    search_pages = 0
    detail_pages = 0
    search_seconds = 0.0
    detail_seconds = 0.0
    full_after_run = 0
    per_source = []
    for s in a.sources:
        pages = s.legs * s.pages_per_leg_used
        search_pages += pages
        detail_pages += s.detail_limit
        search_seconds += pages * s.t_search_page_s
        detail_seconds += s.detail_limit * s.t_detail_page_s
        full = s.detail_limit if s.detail_completeness == "FULL" else 0
        full_after_run += full
        per_source.append({
            "source": s.name, "search_pages": pages, "detail_pages": s.detail_limit,
            "seconds": round(pages * s.t_search_page_s + s.detail_limit * s.t_detail_page_s, 1),
            "max_listings": a.max_results_per_source,
            "full_description_listings": full,
            "card_completeness": s.card_completeness,
            "detail_completeness": s.detail_completeness,
        })
    total_seconds = search_seconds + detail_seconds
    return {
        "search_pages": search_pages,
        "detail_pages": detail_pages,
        "page_ops": search_pages + detail_pages,
        "run_seconds": round(total_seconds, 1),
        "run_minutes": round(total_seconds / 60, 1),
        "max_observations": a.max_results_per_source * len(a.sources),
        "full_description_listings": full_after_run,
        "per_source": per_source,
        "runs_per_day_one_chrome_at_util": round(SECONDS_PER_DAY * a.utilization_target / total_seconds, 1),
    }


def jev_tokens_per_listing(a: Assumptions, *, description_chars: int = 4_000,
                           candidate_bytes: int = 700) -> dict[str, float]:
    """Estimated input tokens for the current two-call flow (bytes / chars_per_token)."""
    listing_view = description_chars + 400
    state = listing_view + candidate_bytes + a.preferences_view_bytes + 300
    focused = state + a.focused_questions_bytes
    final = state + 900 + a.final_question_bytes  # assessments are copied into the state
    tok = lambda b: b / a.chars_per_token  # noqa: E731
    return {
        "focused_call_tokens": round(tok(focused)),
        "final_call_tokens": round(tok(final)),
        "two_call_tokens": round(tok(focused + final)),
        "compact_final_tokens": round(tok(final - description_chars)),
        "single_request_tokens": round(tok(state + a.focused_questions_bytes + a.final_question_bytes)),
    }


def little(rate_per_day: float, service_s: float) -> float:
    """Average concurrency needed: arrival rate times service time."""
    return rate_per_day * service_s / SECONDS_PER_DAY


def tier_requirements(a: Assumptions, v5_per_day: int) -> dict[str, Any]:
    """Upstream volumes and concurrency for ``v5_per_day`` site-observed submissions."""
    attempts = v5_per_day / (1.0 - a.p_unknown - a.p_retryable_failure)
    apply_needed = attempts  # one application per APPLY listing (duplicates removed upstream)
    enriched_needed = apply_needed / a.p_apply_given_enriched
    cheap_pass_needed = enriched_needed  # every cheap-pass listing is enriched in the proposed design
    unique_needed = cheap_pass_needed / a.p_cheap_pass
    observations_needed = unique_needed / (1.0 - a.p_dedupe)
    enrich_pages = enriched_needed * a.p_enrich_needed
    plan = run_plan(a)
    runs_needed = observations_needed / plan["max_observations"]
    search_pages = runs_needed * plan["search_pages"]
    t_search = sum(s.t_search_page_s * s.legs * s.pages_per_leg_used for s in a.sources) / max(1, plan["search_pages"])
    t_detail = sum(s.t_detail_page_s * s.share_of_observations for s in a.sources)
    tokens = jev_tokens_per_listing(a)
    jev_calls_two = enriched_needed * 2
    jev_cost_two = jev_calls_two / 2 * tokens["two_call_tokens"] / 1e6 * a.jev_usd_per_million_input_tokens
    submit_s = a.t_submit_s * (1 - a.p_multistep) + a.t_submit_multistep_s * a.p_multistep
    submit_seconds = attempts * submit_s
    resume_seconds = attempts * a.p_needs_input * a.t_resume_after_input_s
    reconcile_seconds = attempts * a.p_unknown * a.t_reconcile_s * 2
    browser_seconds = submit_seconds + resume_seconds + reconcile_seconds
    human_hours = attempts * a.p_needs_input * a.t_human_block_min / 60.0
    lanes = []
    for window_h in a.day_windows_h:
        per_minute = v5_per_day / (window_h * 60)
        budget_s = window_h * 3600 / v5_per_day
        for svc in a.browser_service_options_s:
            need = attempts * svc / (window_h * 3600) / a.utilization_target
            lanes.append({"window_h": window_h, "service_s": svc, "per_minute": round(per_minute, 3),
                          "completion_budget_s": round(budget_s, 2), "lanes_at_util": round(need, 2),
                          "lanes_ceiling": int(-(-need // 1))})
    return {
        "tier_v5_per_day": v5_per_day,
        "assumed_direct_acceptance_yield": 1.0 - a.p_unknown - a.p_retryable_failure,
        "utilization_target": a.utilization_target,
        "submission_browser_hours": round(submit_seconds / 3600, 2),
        "resume_browser_hours": round(resume_seconds / 3600, 2),
        "reconciliation_browser_hours": round(reconcile_seconds / 3600, 2),
        "total_browser_lanes_at_util_by_window": {
            str(h): __import__("math").ceil(browser_seconds / (h * 3600 * a.utilization_target))
            for h in a.day_windows_h
        },
        "submission_attempts_per_day": round(attempts),
        "apply_listings_needed_per_day": round(apply_needed),
        "enriched_listings_per_day": round(enriched_needed),
        "enrich_page_loads_per_day": round(enrich_pages),
        "unique_listings_per_day": round(unique_needed),
        "observations_per_day": round(observations_needed),
        "search_runs_per_day": round(runs_needed, 1),
        "search_pages_per_day": round(search_pages),
        "discovery_page_ops_per_day": round(search_pages + enrich_pages),
        "discovery_chrome_hours_per_day": round((search_pages * t_search + enrich_pages * t_detail) / 3600, 1),
        "jev_calls_per_day_two_call": round(jev_calls_two),
        "jev_input_tokens_per_day_two_call": round(enriched_needed * tokens["two_call_tokens"]),
        "jev_usd_per_day_two_call": round(jev_cost_two, 2),
        "single_selector_capacity_per_day_at_util": {
            f"{t}s_per_select": round(SECONDS_PER_DAY * a.utilization_target / t)
            for t in (0.5, 1.0, 2.0)
        },
        "browser_hours_per_day": round(browser_seconds / 3600, 1),
        "avg_browser_sessions_little": round(little(attempts, submit_s), 2),
        "human_hours_per_day": round(human_hours, 1),
        "lanes": lanes,
    }


def model(a: Assumptions) -> dict[str, Any]:
    return {
        "run_plan": run_plan(a),
        "jev_tokens_per_listing_estimate": jev_tokens_per_listing(a),
        "tiers": [tier_requirements(a, t) for t in a.tiers],
        "notes": [
            "All tier rows are requirements derived from the assumptions, not achievable throughput.",
            "Market supply of new qualified postings per day is external and unmeasured.",
            "Browser timings are modeled; no Chromium was launched by this harness.",
            "Jev token counts are byte/4 estimates; the provider's usage field is the truth.",
        ],
    }
