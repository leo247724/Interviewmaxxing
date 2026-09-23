"""Jev call-strategy simulation over the fictional population.

The policy (hard constraints, evidence holds, assessment holds, confidence thresholds,
effective decision) mirrors jev-selection ``policy.py``/``rubric.py``/``service.py`` at
0f58421 in plain Python. The judge is a **stub** driven by the fixture truth plus
noise; it models call counts, bytes, latency and cache behaviour, not Jev's accuracy.
Accuracy of any strategy must be measured on candidate-labeled fixtures with real
calls, which this harness never makes.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any

from .config import Assumptions
from .fixtures import CANDIDATE_EVIDENCE, Fixture

APPLY, SKIP, REVIEW = "APPLY", "SKIP", "REVIEW"

HARD = {"LISTING_CLOSED", "DUPLICATE_APPLICATION", "PAY_BELOW_FLOOR", "LOCATION_MISMATCH",
        "REMOTE_NOT_WANTED", "EXCLUDED_KEYWORD", "EXCLUDED_COMPANY"}
SKIP_BLOCKING = {"CONTRADICTORY_EVIDENCE", "LOW_CONFIDENCE", "SUSPECTED_INSTRUCTION_INJECTION",
                 "PROVIDER_ERROR"}
CURABLE = {"MISSING_LISTING_DETAILS", "INCOMPLETE_DESCRIPTION", "LOCATION_UNKNOWN"}
INCURABLE = {"MISSING_PROFILE", "SUSPECTED_INSTRUCTION_INJECTION"}

STRATEGIES = [
    "baseline_two_call",
    "gate_then_two_call",
    "gate_compact_final",
    "gate_combined_selective",
    "gate_single_request",
]


# --- policy (mirror of J2, stdlib) -------------------------------------------------------


def compensation_status(listing: dict[str, Any], floor: float = 100_000.0) -> str:
    pay = listing.get("compensation")
    if not pay or (pay.get("minimum") is None and pay.get("maximum") is None):
        return "UNKNOWN"
    if pay.get("currency") != "USD":
        return "NONCOMPARABLE"
    period = pay.get("period")
    factor = {"YEAR": 1.0, "MONTH": 12.0}.get(period)
    if factor is None:
        return "NONCOMPARABLE"
    if pay.get("maximum") is not None:
        return "MEETS_FLOOR" if pay["maximum"] * factor >= floor else "BELOW_FLOOR"
    return "MEETS_FLOOR" if pay["minimum"] * factor >= floor else "NONCOMPARABLE"


def location_status(listing: dict[str, Any]) -> str:
    arr = listing.get("work_arrangement")
    if arr == "REMOTE":
        stated = (listing.get("remote_eligibility") or "").strip().lower()
        return "REMOTE_REGION_MATCH" if stated in {"united states", "us", "usa"} else "REMOTE_NEEDS_ELIGIBILITY"
    if arr == "UNKNOWN" or not listing.get("location"):
        return "UNKNOWN"
    if "austin" in (listing.get("location") or "").lower() and arr in ("ONSITE", "HYBRID"):
        return "ONSITE_ACCEPTED"
    return "ONSITE_MISMATCH"


def location_tier(status: str) -> str:
    if status == "ONSITE_ACCEPTED":
        return "PREFERRED"
    if status in ("REMOTE_REGION_MATCH", "REMOTE_NEEDS_ELIGIBILITY"):
        return "SECONDARY"
    return "UNRANKED"


def hard_constraint_holds(listing: dict[str, Any], comp: str, loc: str,
                          existing_application: bool = False) -> list[str]:
    holds = []
    if listing.get("status") == "CLOSED":
        holds.append("LISTING_CLOSED")
    if existing_application:
        holds.append("DUPLICATE_APPLICATION")
    if comp == "BELOW_FLOOR":
        holds.append("PAY_BELOW_FLOOR")
    if loc == "ONSITE_MISMATCH":
        holds.append("LOCATION_MISMATCH")
    return holds


def evidence_holds(listing: dict[str, Any], loc: str, *, has_candidate: bool,
                   max_chars: int) -> list[str]:
    holds = []
    if not has_candidate:
        holds.append("MISSING_PROFILE")
    text = (listing.get("description") or "").strip()
    if not text:
        holds.append("MISSING_LISTING_DETAILS")
    elif listing.get("description_completeness") != "FULL" or len(text) > max_chars:
        holds.append("INCOMPLETE_DESCRIPTION")
    if loc == "UNKNOWN":
        holds.append("LOCATION_UNKNOWN")
    return holds


NEGATIVE = {
    "role_match": {"mismatch"},
    "seniority_match": {"below_target_level", "above_target_level"},
    "qualification_match": {"does_not_meet"},
    "location_eligibility": {"not_eligible"},
    "preference_match": {"conflicts"},
}
INSUFFICIENT = {"role_match", "seniority_match", "qualification_match"}
STRONG = {"role_match": "match", "seniority_match": "at_target_level",
          "qualification_match": "meets", "location_eligibility": "eligible"}


def assessment_holds(assessments: dict[str, Answer], final: str) -> list[str]:
    holds = []
    for name in INSUFFICIENT:
        if assessments[name].choice == "insufficient_evidence":
            holds.append("INSUFFICIENT_EVIDENCE")
    if assessments["location_eligibility"].choice == "ambiguous":
        holds.append("ELIGIBILITY_AMBIGUOUS")
    if assessments["listing_consistency"].choice == "contradictory":
        holds.append("CONTRADICTORY_EVIDENCE")
    if final == APPLY and assessments["role_match"].choice == "adjacent":
        holds.append("ROLE_FOCUS_UNCONFIRMED")
    if final == APPLY and any(assessments[n].choice in bad for n, bad in NEGATIVE.items()):
        holds.append("CONTRADICTORY_EVIDENCE")
    if final == SKIP and all(assessments[n].choice == v for n, v in STRONG.items()):
        holds.append("CONTRADICTORY_EVIDENCE")
    return holds


def effective_decision(model_choice: str | None, holds: list[str]) -> str:
    reasons = set(holds)
    if reasons & HARD:
        return SKIP
    if model_choice is None:
        return REVIEW
    if model_choice == APPLY and reasons:
        return REVIEW
    if model_choice == SKIP and reasons & SKIP_BLOCKING:
        return REVIEW
    return model_choice


# --- stub judge --------------------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    choice: str
    confidence: float


class StubJudge:
    """Answers from the fixture truth with seeded noise. **Not a model of Jev.**"""

    def __init__(self, seed: int, *, p_flip: float = 0.10, p_independent_final_noise: float = 0.15) -> None:
        self.rng = random.Random(seed)
        self.p_flip = p_flip
        self.p_final_noise = p_independent_final_noise

    def _conf(self, lo: float, hi: float) -> float:
        return round(self.rng.uniform(lo, hi), 3)

    def focused(self, fx: Fixture) -> dict[str, Answer]:
        t = fx.truth
        listing = fx.listing
        no_text = not (listing.get("description") or "").strip()
        partial = listing.get("description_completeness") == "PARTIAL"

        def flip(value: str, options: list[str]) -> str:
            if self.rng.random() < self.p_flip:
                return self.rng.choice([o for o in options if o != value])
            return value

        if no_text:
            role = Answer("insufficient_evidence", self._conf(0.6, 0.9))
            quals = Answer("insufficient_evidence", self._conf(0.6, 0.9))
        else:
            role_choice = flip(t.role, ["match", "adjacent", "mismatch"])
            role = Answer(role_choice, self._conf(0.55, 0.85) if partial else self._conf(0.75, 0.98))
            quals = Answer(flip(t.quals, ["meets", "partially_meets", "does_not_meet"]),
                           self._conf(0.6, 0.95))
        seniority = Answer(flip(t.seniority, ["at_target_level", "below_target_level", "above_target_level"]),
                           self._conf(0.7, 0.97))
        lb = t.location_bucket
        if lb in ("austin_onsite", "austin_hybrid", "remote_us"):
            elig = Answer("eligible", self._conf(0.8, 0.98))
        elif lb in ("remote_limited", "austin_unstated", "unknown"):
            elig = Answer("ambiguous", self._conf(0.6, 0.9))
        else:
            elig = Answer("not_eligible", self._conf(0.8, 0.98))
        pref = Answer("aligned", self._conf(0.7, 0.95))
        cons = Answer("contradictory" if self.rng.random() < 0.03 else "consistent", self._conf(0.7, 0.97))
        return {"role_match": role, "seniority_match": seniority, "qualification_match": quals,
                "location_eligibility": elig, "preference_match": pref, "listing_consistency": cons}

    def final(self, fx: Fixture, assessments: dict[str, Answer], *, sees_assessments: bool) -> Answer:
        if not sees_assessments:
            # Sibling questions see the SAME input, never one another's answers.
            t = fx.truth
            strong = t.role == "match" and t.quals == "meets" and t.seniority == "at_target_level" and t.location_bucket in ("austin_onsite", "austin_hybrid", "remote_us")
            negative = t.role == "mismatch" or t.quals == "does_not_meet"
        else:
            strong = all(assessments[n].choice == v for n, v in STRONG.items())
            negative = any(assessments[n].choice in bad for n, bad in NEGATIVE.items())
        if strong:
            choice, lo, hi = APPLY, 0.7, 0.97
        elif negative:
            choice, lo, hi = SKIP, 0.6, 0.95
        else:
            choice, lo, hi = REVIEW, 0.5, 0.85
        if not sees_assessments and self.rng.random() < self.p_final_noise:
            choice = self.rng.choice([c for c in (APPLY, SKIP, REVIEW) if c != choice])
            lo, hi = 0.45, 0.8
        return Answer(choice, self._conf(lo, hi))


# --- request sizing ------------------------------------------------------------------------


def listing_view(listing: dict[str, Any], *, max_chars: int, with_description: bool = True) -> dict[str, Any]:
    text = listing.get("description")
    if text and len(text) > max_chars:
        text = text[:max_chars]
    return {
        "title": listing.get("title"), "company": listing.get("company"),
        "location": listing.get("location"), "work_arrangement": listing.get("work_arrangement"),
        "remote_eligibility_as_stated": listing.get("remote_eligibility"),
        "compensation_as_stated": (listing.get("compensation") or {}).get("raw_text"),
        "description": text if with_description else None,
        "description_complete": listing.get("description_completeness") == "FULL",
        "source": listing.get("source"),
    }


def request_bytes(a: Assumptions, listing: dict[str, Any], *, questions_bytes: int,
                  with_description: bool, with_assessments: bool) -> int:
    state = {
        "listing": listing_view(listing, max_chars=a.max_description_chars, with_description=with_description),
        "candidate": {k: CANDIDATE_EVIDENCE[k] for k in ("verified_facts", "experience", "education")},
        "checks": {"pay_vs_floor": "UNKNOWN", "location": "ONSITE_ACCEPTED", "location_tier": "PREFERRED",
                   "location_priority_reason": "location priority: onsite/hybrid in an accepted location is the preferred tier (STRONGLY_PREFER_ONSITE_HYBRID)",
                   "policy_holds": []},
    }
    body = len(json.dumps(state, ensure_ascii=False).encode()) + a.preferences_view_bytes + questions_bytes + 40
    if with_assessments:
        body += 6 * 150
    return body


# --- one decision --------------------------------------------------------------------------


@dataclass
class Decision:
    listing_id: str
    strategy: str
    effective: str
    model_choice: str | None
    holds: list[str]
    calls: int
    attempted_calls: int
    input_bytes: int
    latency_s: float
    deferred_to_enrichment: bool = False
    skipped_reason: str | None = None
    tier: str = "UNRANKED"
    apply_probability: float = 0.0


@dataclass
class Provider:
    """Counts attempts (billed or not, unknown) separately from successful calls.

    Fictional latency is lognormal with median t_jev_call_s and fixed sigma .35;
    report percentiles are sampled outputs, not a configurable or measured p95.
    """

    a: Assumptions
    rng: random.Random
    calls: int = 0
    attempts: int = 0
    failures: int = 0

    def call(self) -> tuple[int, float, bool]:
        """Returns (attempts used, latency seconds, ok). ``ok`` is False when every
        attempt failed transiently (the client then raises JevProviderError)."""
        used = 0
        latency = 0.0
        while True:
            used += 1
            self.attempts += 1
            latency += self.rng.lognormvariate(0, 0.35) * self.a.t_jev_call_s
            if self.rng.random() >= self.a.p_transient_provider_failure:
                self.calls += 1
                return used, latency, True
            self.failures += 1
            if used >= self.a.jev_max_attempts:
                return used, latency, False
            latency += (0.5, 2.0)[min(used - 1, 1)]


def decide(fx: Fixture, strategy: str, a: Assumptions, judge: StubJudge, provider: Provider,
           *, has_candidate: bool = True, existing_application: bool = False) -> Decision:
    listing = fx.listing
    comp = compensation_status(listing)
    loc = location_status(listing)
    tier = location_tier(loc)
    holds = evidence_holds(listing, loc, has_candidate=has_candidate, max_chars=a.max_description_chars)
    hard = hard_constraint_holds(listing, comp, loc, existing_application)
    if hard:
        return Decision(fx.id, strategy, SKIP, None, hard + holds, 0, 0, 0, 0.0,
                        skipped_reason="hard_constraint", tier=tier)
    gate = strategy != "baseline_two_call"
    if gate and set(holds) & INCURABLE:
        return Decision(fx.id, strategy, REVIEW, None, holds, 0, 0, 0, 0.0,
                        skipped_reason="incurable_hold", tier=tier)
    if gate and set(holds) & CURABLE:
        return Decision(fx.id, strategy, REVIEW, None, holds, 0, 0, 0, 0.0,
                        deferred_to_enrichment=True, skipped_reason="curable_hold", tier=tier)

    assessments = judge.focused(fx)
    calls = attempts = 0
    latency = 0.0
    input_bytes = 0

    class _ProviderDown(Exception):
        pass

    def one_call(qbytes: int, *, with_description: bool, with_assessments: bool) -> None:
        nonlocal calls, attempts, latency, input_bytes
        used, lat, ok = provider.call()
        attempts += used
        latency += lat
        input_bytes += used * request_bytes(a, listing, questions_bytes=qbytes,
                                            with_description=with_description,
                                            with_assessments=with_assessments)
        if not ok:
            raise _ProviderDown()
        calls += 1

    try:
        if strategy in ("baseline_two_call", "gate_then_two_call"):
            one_call(a.focused_questions_bytes, with_description=True, with_assessments=False)
            one_call(a.final_question_bytes, with_description=True, with_assessments=True)
            final = judge.final(fx, assessments, sees_assessments=True)
        elif strategy == "gate_compact_final":
            one_call(a.focused_questions_bytes, with_description=True, with_assessments=False)
            one_call(a.final_question_bytes, with_description=False, with_assessments=True)
            final = judge.final(fx, assessments, sees_assessments=True)
        elif strategy == "gate_single_request":
            one_call(a.focused_questions_bytes + a.final_question_bytes, with_description=True,
                     with_assessments=False)
            final = judge.final(fx, assessments, sees_assessments=False)
        elif strategy == "gate_combined_selective":
            one_call(a.focused_questions_bytes + a.final_question_bytes, with_description=True,
                     with_assessments=False)
            final = judge.final(fx, assessments, sees_assessments=False)
            threshold = {APPLY: a.apply_confidence, SKIP: a.skip_confidence}.get(final.choice)
            contradiction = bool(assessment_holds(assessments, final.choice))
            if (threshold is not None and final.confidence < threshold) or contradiction:
                one_call(a.final_question_bytes, with_description=True, with_assessments=True)
                final = judge.final(fx, assessments, sees_assessments=True)
        else:
            raise ValueError(strategy)
    except _ProviderDown:
        # JevProviderError path: REVIEW with a PROVIDER_ERROR hold, never cached (service.py:288-307).
        return Decision(fx.id, strategy, REVIEW, None, [*holds, "PROVIDER_ERROR"], calls, attempts,
                        input_bytes, latency, skipped_reason="provider_error", tier=tier)

    holds = holds + assessment_holds(assessments, final.choice)
    threshold = {APPLY: a.apply_confidence, SKIP: a.skip_confidence}.get(final.choice)
    if threshold is not None and final.confidence < threshold:
        holds.append("LOW_CONFIDENCE")
    effective = effective_decision(final.choice, holds)
    return Decision(fx.id, strategy, effective, final.choice, holds, calls, attempts, input_bytes,
                    latency, tier=tier, apply_probability=final.confidence if final.choice == APPLY else 0.0)


# --- population run ------------------------------------------------------------------------


@dataclass
class StrategyReport:
    strategy: str
    listings: int
    reached_jev: int
    calls: int
    attempted_calls: int
    input_tokens: int
    usd: float
    mean_latency_s: float
    p95_latency_s: float
    outcomes: dict[str, int]
    deferred_to_enrichment: int
    apply_agreement_with_baseline: float | None = None


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def run_strategies(fixtures: list[Fixture], a: Assumptions, seed: int = 7) -> dict[str, Any]:
    reports: list[StrategyReport] = []
    decisions: dict[str, dict[str, Decision]] = {}
    for strategy in STRATEGIES:
        # Stable per-listing noise gives gating variants comparable focused answers.
        out = [decide(fx, strategy, a, StubJudge(seed + i),
                      Provider(a, random.Random(seed + i + 10000)))
               for i, fx in enumerate(fixtures)]
        decisions[strategy] = {d.listing_id: d for d in out}
        reached = [d for d in out if d.attempted_calls > 0]
        latencies = [d.latency_s for d in reached]
        tokens = sum(d.input_bytes for d in out) / a.chars_per_token
        outcomes: dict[str, int] = {}
        for d in out:
            key = d.effective + ("(deferred)" if d.deferred_to_enrichment else "")
            outcomes[key] = outcomes.get(key, 0) + 1
        reports.append(StrategyReport(
            strategy=strategy, listings=len(out), reached_jev=len(reached),
            calls=sum(d.calls for d in out), attempted_calls=sum(d.attempted_calls for d in out),
            input_tokens=round(tokens),
            usd=round(tokens / 1e6 * a.jev_usd_per_million_input_tokens, 4),
            mean_latency_s=round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
            p95_latency_s=round(_p95(latencies), 3),
            outcomes=dict(sorted(outcomes.items())),
            deferred_to_enrichment=sum(1 for d in out if d.deferred_to_enrichment),
        ))
    base = decisions["baseline_two_call"]
    for r in reports:
        if r.strategy == "baseline_two_call":
            continue
        mine = decisions[r.strategy]
        compared = [i for i in base if base[i].calls > 0 and mine[i].calls > 0]
        if compared:
            agree = sum(1 for i in compared if (base[i].effective == APPLY) == (mine[i].effective == APPLY))
            r.apply_agreement_with_baseline = round(agree / len(compared), 3)
    return {
        "note": "Judge is a fixture-driven stub; only calls/bytes/latency/cache mechanics are meaningful. "
                "apply_agreement_with_baseline measures the harness's own combination logic, not Jev.",
        "strategies": [r.__dict__ for r in reports],
    }


# --- cache and invalidation ----------------------------------------------------------------


def job_evidence_hash(listing: dict[str, Any], *, include_application_url: bool, max_chars: int) -> str:
    import hashlib
    view = listing_view(listing, max_chars=max_chars)
    data = {"jev_view": view, "compensation": listing.get("compensation"), "status": listing.get("status")}
    if include_application_url:
        data["application_url"] = listing.get("application_url")
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def cache_scenarios(fixtures: list[Fixture], a: Assumptions, seed: int = 11,
                    redecide_budget_calls: int = 400) -> dict[str, Any]:
    rng = random.Random(seed)
    judge = StubJudge(seed)
    provider = Provider(a, random.Random(seed + 1))
    decided = {fx.id: decide(fx, "gate_then_two_call", a, judge, provider) for fx in fixtures}
    cached = {fx.id: fx for fx in fixtures
              if decided[fx.id].model_choice is not None
              and "PROVIDER_ERROR" not in decided[fx.id].holds}
    n = len(cached)

    # (a) re-observation changes only posted_text
    hits_a = 0
    for fx in cached.values():
        before = job_evidence_hash(fx.listing, include_application_url=True, max_chars=a.max_description_chars)
        after_listing = {**fx.listing, "posted_text": "1 day ago"}
        after = job_evidence_hash(after_listing, include_application_url=True, max_chars=a.max_description_chars)
        hits_a += before == after

    # (b) enrichment adds an application_url
    miss_current = hit_proposed = 0
    affected = 0
    for fx in cached.values():
        if fx.listing.get("application_url"):
            continue
        affected += 1
        enriched = {**fx.listing, "application_url": "https://boards.greenhouse.example/x/jobs/1"}
        cur_before = job_evidence_hash(fx.listing, include_application_url=True, max_chars=a.max_description_chars)
        cur_after = job_evidence_hash(enriched, include_application_url=True, max_chars=a.max_description_chars)
        pro_before = job_evidence_hash(fx.listing, include_application_url=False, max_chars=a.max_description_chars)
        pro_after = job_evidence_hash(enriched, include_application_url=False, max_chars=a.max_description_chars)
        miss_current += cur_before != cur_after
        hit_proposed += pro_before == pro_after

    # (c) preference edit invalidates everything; prioritized re-decide under a call budget
    order = sorted(cached.values(), key=lambda fx: (
        0 if decided[fx.id].effective == APPLY else 1 if decided[fx.id].effective == REVIEW else 2,
        {"PREFERRED": 0, "SECONDARY": 1}.get(decided[fx.id].tier, 2),
    ))
    budget_listings = redecide_budget_calls // (2 * a.jev_max_attempts)
    covered = order[:budget_listings]
    covered_apply = sum(1 for fx in covered if decided[fx.id].effective == APPLY)
    total_apply = sum(1 for fx in cached.values() if decided[fx.id].effective == APPLY)
    covered_review = sum(1 for fx in covered if decided[fx.id].effective == REVIEW)
    total_review = sum(1 for fx in cached.values() if decided[fx.id].effective == REVIEW)

    return {
        "cached_decisions": n,
        "reobservation_posted_text_only": {"cache_hits": hits_a, "of": n,
                                           "note": "posted_text is not in job_evidence; hit under current rule"},
        "enrichment_adds_application_url": {"affected": affected, "misses_current_rule": miss_current,
                                            "hits_proposed_rule": hit_proposed,
                                            "note": "semantic cache hit only: execution URL/version and duplicate checks must rerun"},
        "preference_edit": {"invalidated": n, "redecide_budget_calls": redecide_budget_calls,
                            "listings_redecided_within_budget": len(covered),
                            "apply_covered": f"{covered_apply}/{total_apply}",
                            "review_covered": f"{covered_review}/{total_review}",
                            "note": "reserve max_attempts for each of two calls before scheduling; prioritized APPLY/REVIEW then tier"},
        "candidate_fact_version_bump": {"invalidated": n, "note": "candidate_evidence_hash changes with any verified-fact edit"},
        "rng_check": rng.random(),
    }
