"""Assumptions for the capacity model and simulations, each with a provenance label.

``observed`` values are read from code constants of the sibling worktrees (cited) or
from primary provider documentation. ``modeled`` values are guesses that the
simulations expose as parameters; change them in a JSON file and rerun. ``external``
values are set by the market or by third parties and were not measured here.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

OBSERVED = "observed"
MODELED = "modeled"
EXTERNAL = "external"


@dataclass
class SourceProfile:
    """One job source as the J1 adapters actually drive it (job-ingestion 1d8f2ee)."""

    name: str
    legs: int
    """Searches per run with the eight default seeds (``build_legs``)."""
    pages_per_leg_used: int
    """Pages a leg typically visits before its budget is spent (modeled; bound is 3)."""
    page_size: int
    detail_limit: int
    """Detail pages opened per source per run (``JobSearchService.detail_limit``)."""
    card_completeness: str
    """Description completeness of a card-only listing: NONE or PARTIAL."""
    detail_completeness: str
    """Completeness after the detail page: FULL, PARTIAL, or NONE (LinkedIn background)."""
    t_search_page_s: float
    t_detail_page_s: float
    share_of_observations: float
    """Modeled share of V1 observations this source contributes."""
    p_apply_link_on_detail: float
    """Share of detail pages that expose a job-specific employer ATS apply link."""


def default_sources() -> list[SourceProfile]:
    return [
        SourceProfile("linkedin", legs=4, pages_per_leg_used=2, page_size=25, detail_limit=10,
                      card_completeness="NONE", detail_completeness="NONE",
                      t_search_page_s=6.0, t_detail_page_s=8.0, share_of_observations=0.35,
                      p_apply_link_on_detail=0.6),
        SourceProfile("builtin", legs=16, pages_per_leg_used=1, page_size=25, detail_limit=10,
                      card_completeness="NONE", detail_completeness="FULL",
                      t_search_page_s=5.0, t_detail_page_s=5.0, share_of_observations=0.20,
                      p_apply_link_on_detail=0.0),
        SourceProfile("indeed", legs=16, pages_per_leg_used=1, page_size=10, detail_limit=10,
                      card_completeness="NONE", detail_completeness="FULL",
                      t_search_page_s=5.0, t_detail_page_s=5.0, share_of_observations=0.30,
                      p_apply_link_on_detail=0.4),
        SourceProfile("google", legs=6, pages_per_leg_used=1, page_size=10, detail_limit=10,
                      card_completeness="PARTIAL", detail_completeness="PARTIAL",
                      t_search_page_s=6.0, t_detail_page_s=4.0, share_of_observations=0.15,
                      p_apply_link_on_detail=0.5),
    ]


@dataclass
class Assumptions:
    # --- funnel ratios (modeled unless noted) ---
    p_dedupe: float = 0.35
    p_cheap_pass: float = 0.55
    p_enrich_needed: float = 0.80
    p_apply_given_enriched: float = 0.25
    p_needs_input: float = 0.30
    p_unknown: float = 0.05
    p_retryable_failure: float = 0.10
    p_multistep: float = 0.30
    p_global_reuse: float = 0.70
    """Share of answered questions the user chooses to save with GLOBAL reuse."""

    # --- service times, seconds ---
    t_jev_call_s: float = 0.5
    t_packet_s: float = 0.05
    t_submit_s: float = 90.0
    t_submit_multistep_s: float = 240.0
    t_resume_after_input_s: float = 45.0
    t_reconcile_s: float = 45.0
    t_human_block_min: float = 5.0
    tenant_spacing_s: float = 120.0
    search_interval_min: float = 30.0
    human_hours_per_day: float = 8.0

    # --- resources ---
    browser_slots: int = 1
    jev_inflight: int = 1
    chrome_cmd_parallel: int = 1
    enrich_pages_per_source_per_hour: int = 120
    utilization_target: float = 0.70

    # --- provider pricing and limits (observed from primary docs, 2026-09-22) ---
    jev_usd_per_million_input_tokens: float = 0.042
    jev_usd_per_million_output_tokens: float = 0.0
    jev_context_tokens: int = 32_000
    chars_per_token: float = 4.0
    p_transient_provider_failure: float = 0.02
    jev_max_attempts: int = 3
    apply_confidence: float = 0.8
    skip_confidence: float = 0.8
    max_description_chars: int = 12_000

    # --- observed request sizes (bytes) from jev-selection 0f58421 ---
    focused_questions_bytes: int = 6089
    final_question_bytes: int = 1931
    preferences_view_bytes: int = 899

    # --- discovery ---
    max_results_per_source: int = 50
    sources: list[SourceProfile] = field(default_factory=default_sources)

    # --- targets ---
    tiers: list[int] = field(default_factory=lambda: [1000, 5000, 10000])
    day_windows_h: list[float] = field(default_factory=lambda: [24.0, 8.0])
    browser_service_options_s: list[float] = field(default_factory=lambda: [30.0, 60.0, 120.0])

    def __post_init__(self) -> None:
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, (int, float)):
                if not math.isfinite(value) or value < 0:
                    raise ValueError(f"{f.name} must be finite and nonnegative")
                if f.name.startswith("p_") and value > 1:
                    raise ValueError(f"{f.name} must be a probability")
        if self.p_unknown + self.p_retryable_failure >= 1:
            raise ValueError("direct acceptance yield must be positive")
        if not 0 < self.utilization_target <= 1 or not 0 < self.human_hours_per_day <= 15:
            raise ValueError("invalid utilization or human window (starts 09:00)")
        if any(getattr(self, name) <= 0 for name in (
            "p_apply_given_enriched", "p_cheap_pass", "chars_per_token", "browser_slots",
            "jev_inflight", "chrome_cmd_parallel", "enrich_pages_per_source_per_hour",
            "jev_max_attempts", "search_interval_min", "max_results_per_source")) or self.p_dedupe >= 1:
            raise ValueError("resource counts, rates and divisors must be positive")
        if not self.day_windows_h or any(not 0 < h <= 24 for h in self.day_windows_h):
            raise ValueError("day windows must be in (0, 24]")

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Assumptions:
        data = json.loads(text)
        sources = [SourceProfile(**s) for s in data.pop("sources", [])] or default_sources()
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown assumption fields: {unknown}")
        return cls(sources=sources, **data)

    @classmethod
    def load(cls, path: Path | None) -> Assumptions:
        if path is None:
            return cls()
        return cls.from_json(Path(path).read_text("utf-8"))

    def source(self, name: str) -> SourceProfile:
        for s in self.sources:
            if s.name == name:
                return s
        raise KeyError(name)


PROVENANCE: dict[str, tuple[str, str]] = {
    "p_dedupe": (MODELED, "share of observations that repeat a known listing; measure from JobStore.observations"),
    "p_cheap_pass": (MODELED, "share surviving closed/pay/location/excluded-term gates"),
    "p_enrich_needed": (MODELED, "0.80 is a modeled bound: 40 detail attempts/200 cards do not establish 40 FULL descriptions"),
    "p_apply_given_enriched": (MODELED, "needs the candidate-labeled fixture set; guess"),
    "p_needs_input": (MODELED, "first submission per tenant stopping in NEEDS_INPUT at least once; guess"),
    "p_unknown": (MODELED, "submits ending SUBMISSION_UNKNOWN; guess"),
    "p_retryable_failure": (MODELED, "runs ending FAILED_RETRYABLE before submit; guess"),
    "p_multistep": (MODELED, "share of multi-step (e.g. Workday) forms; guess"),
    "p_global_reuse": (MODELED, "user chooses GLOBAL reuse for an answer; guess"),
    "t_jev_call_s": (MODELED, "fictional per-attempt lognormal median; fixed sigma .35; no live latency or configurable p95"),
    "t_packet_s": (MODELED, "FactualPacketResolver is CPU-bound; sub-50 ms guess"),
    "t_submit_s": (MODELED, "single-page application incl. Chromium launch (session.py:76-110); unmeasured"),
    "t_submit_multistep_s": (MODELED, "multi-step form; unmeasured; RunLimits.max_steps=12"),
    "t_resume_after_input_s": (MODELED, "re-inspect and continue after NEEDS_INPUT; unmeasured"),
    "t_reconcile_s": (MODELED, "browser recheck; unmeasured"),
    "t_human_block_min": (MODELED, "Astra relay: 5 min per human block"),
    "tenant_spacing_s": (MODELED, "courtesy spacing between submissions to one ATS tenant"),
    "search_interval_min": (MODELED, "how often a full search run starts"),
    "human_hours_per_day": (MODELED, "when the user answers questions"),
    "browser_slots": (OBSERVED, "exactly 1 today: profile flock (runner.py:132-148) + Dispatcher (executor.py:96-143)"),
    "jev_inflight": (OBSERVED, "1 today: single-thread decision pool (jobs_api.py:360)"),
    "chrome_cmd_parallel": (OBSERVED, "1 today: sources sequential (search.py:86-92); one action per session until proven"),
    "enrich_pages_per_source_per_hour": (MODELED, "per-source page budget for the proposed enrich lane"),
    "utilization_target": (MODELED, "Astra relay: 70% utilization for lane sizing"),
    "jev_usd_per_million_input_tokens": (OBSERVED, "openrouter.ai/typesafe/jev-1.13/api: $0.042 / $0 per 1M"),
    "jev_usd_per_million_output_tokens": (OBSERVED, "output free (same page)"),
    "jev_context_tokens": (OBSERVED, "32K context on the model page; tutorial: 32k for state plus the longest question"),
    "chars_per_token": (MODELED, "byte/4 estimate; provider usage is the truth"),
    "p_transient_provider_failure": (MODELED, "timeouts/5xx share; guess"),
    "jev_max_attempts": (OBSERVED, "JevClient.max_attempts default 3 (jev.py:281)"),
    "apply_confidence": (OBSERVED, "SelectionPolicy.apply_confidence 0.8 (rubric.py:33)"),
    "skip_confidence": (OBSERVED, "SelectionPolicy.skip_confidence 0.8 (rubric.py:34)"),
    "max_description_chars": (OBSERVED, "MAX_DESCRIPTION_CHARS (evidence.py:31)"),
    "focused_questions_bytes": (OBSERVED, "json bytes of FOCUSED_QUESTIONS at 0f58421"),
    "final_question_bytes": (OBSERVED, "json bytes of FINAL_QUESTION at 0f58421"),
    "preferences_view_bytes": (OBSERVED, "json bytes of jev_preferences_view(SelectionPreferences()) at 0f58421"),
    "max_results_per_source": (OBSERVED, "JobSearchQuery.max_results_per_source default 50 (discovery.py:181)"),
    "sources": (OBSERVED, "legs/page sizes/detail limits from the J1 adapters; timings modeled"),
    "tiers": (MODELED, "the user's targets"),
    "day_windows_h": (MODELED, "Astra relay: 24 h and 8 h windows"),
    "browser_service_options_s": (MODELED, "Astra relay: 30/60/120 s hypothetical submission times"),
}


def provenance_table() -> list[dict[str, Any]]:
    a = Assumptions()
    rows = []
    for f in fields(a):
        label, note = PROVENANCE.get(f.name, (MODELED, ""))
        value: Any = getattr(a, f.name)
        if f.name == "sources":
            value = [s.name for s in value]
        rows.append({"field": f.name, "default": value, "label": label, "note": note})
    return rows
