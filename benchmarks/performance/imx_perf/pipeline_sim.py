"""Discrete-event simulation of the seven-stage pipeline on a virtual clock.

No threads, no browser, no network. Resources are counted semaphores with priority
queues; listings come from the fictional population; the judge is the same stub as
``selection_sim``. Outputs are the six volumes per day, resource utilization, queue
depths, stage latencies, Jev cost, human hours and the binding resource.

Every result is **modeled**. The point is to show where the ceilings are under the
stated assumptions and to give a tool for arguing about them, not to predict live
employer throughput.
"""

from __future__ import annotations

import heapq
import random
from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

from .config import Assumptions
from .fixtures import BASE_QUESTIONS, Fixture, Population
from .selection_sim import APPLY, REVIEW, Provider, StubJudge, decide

Proc = Generator[tuple, Any, None]


# --- tiny event loop ---------------------------------------------------------------------


class Resource:
    def __init__(self, env: Env, name: str, capacity: int) -> None:
        self.env = env
        self.name = name
        self.capacity = capacity
        self.active = 0
        self.waiting: list[tuple[int, int, float, Proc]] = []
        self.busy_time = 0.0
        self.last_change = 0.0
        self.max_queue = 0
        self.granted = 0

    def _account(self) -> None:
        now = self.env.now
        self.busy_time += self.active * (now - self.last_change)
        self.last_change = now

    def acquire(self, proc: Proc, priority: int) -> None:
        self._account()
        if self.active < self.capacity:
            self.active += 1
            self.granted += 1
            self.env.resume(proc, None)
        else:
            heapq.heappush(self.waiting, (-priority, self.env.seq(), self.env.now, proc))
            self.max_queue = max(self.max_queue, len(self.waiting))

    def release(self) -> None:
        self._account()
        self.active -= 1
        if self.waiting:
            aged = [x for x in self.waiting if self.env.now - x[2] >= 900]
            if aged:
                selected = min(aged, key=lambda x: x[1])
                self.waiting.remove(selected)
                heapq.heapify(self.waiting)
                _, _, _, proc = selected
            else:
                _, _, _, proc = heapq.heappop(self.waiting)
            self.active += 1
            self.granted += 1
            self.env.resume(proc, None)

    def utilization(self) -> float:
        self._account()
        if self.env.now <= 0 or self.capacity == 0:
            return 0.0
        return self.busy_time / (self.env.now * self.capacity)


class Env:
    def __init__(self) -> None:
        self.now = 0.0
        self._queue: list[tuple[float, int, Proc, Any]] = []
        self._seq = 0

    def seq(self) -> int:
        self._seq += 1
        return self._seq

    def resume(self, proc: Proc, value: Any, delay: float = 0.0) -> None:
        heapq.heappush(self._queue, (self.now + delay, self.seq(), proc, value))

    def process(self, proc: Proc) -> None:
        self.resume(proc, None)

    def _step(self, proc: Proc, value: Any) -> None:
        try:
            cmd = proc.send(value)
        except StopIteration:
            return
        kind = cmd[0]
        if kind == "timeout":
            self.resume(proc, None, delay=float(cmd[1]))
        elif kind == "acquire":
            cmd[1].acquire(proc, int(cmd[2]) if len(cmd) > 2 else 0)
        else:
            raise ValueError(cmd)

    def run(self, until: float) -> None:
        while self._queue and self._queue[0][0] <= until:
            t, _, proc, value = heapq.heappop(self._queue)
            self.now = t
            self._step(proc, value)
        self.now = until


# --- scenario ----------------------------------------------------------------------------


@dataclass
class Scenario:
    supply_new_per_day: int = 200
    """External: new unique listings the sources can show per day."""
    days: float = 1.0
    strategy: str = "gate_then_two_call"
    browser_slots: int | None = None
    jev_inflight: int | None = None
    chrome_cmd_parallel: int | None = None
    enrich_lane: bool = True
    """Proposed design: enrich card-only listings after cheap gates (Phase 3)."""
    seed: int = 5
    label: str = ""


@dataclass
class Counters:
    observations: int = 0
    unique: int = 0
    dedupe_hits: int = 0
    gated_out: int = 0
    enrich_pages: int = 0
    enriched_full: int = 0
    deferred_no_text: int = 0
    jev_listings: int = 0
    jev_calls: int = 0
    jev_attempts: int = 0
    jev_input_bytes: int = 0
    apply: int = 0
    review: int = 0
    skip: int = 0
    applications: int = 0
    needs_input: int = 0
    questions_asked: int = 0
    questions_reused: int = 0
    submitted_observed: int = 0
    submission_unknown: int = 0
    reconciled_submitted: int = 0
    reconcile_checks: int = 0
    failed_retryable: int = 0
    human_seconds: float = 0.0
    search_pages: int = 0


class Simulation:
    def __init__(self, a: Assumptions, sc: Scenario) -> None:
        self.a = a
        self.sc = sc
        self.env = Env()
        self.rng = random.Random(sc.seed)
        self.pop = Population(a, sc.seed)
        self.judge = StubJudge(sc.seed)
        self.provider = Provider(a, random.Random(sc.seed + 1))
        self.c = Counters()
        self.chrome = Resource(self.env, "chrome", sc.chrome_cmd_parallel or a.chrome_cmd_parallel)
        self.jev = Resource(self.env, "jev", sc.jev_inflight or a.jev_inflight)
        self.browser = Resource(self.env, "browser_slots", sc.browser_slots or a.browser_slots)
        self.human = Resource(self.env, "human", 1)
        self.tenants: dict[str, Resource] = {}
        self.answered_global: set[str] = set()
        self.answered_tenant: set[tuple[str, str]] = set()
        self.seen_ids: set[str] = set()
        self.seen_keys: set[str] = set()
        self.supply_left = sc.supply_new_per_day
        self.day_index = 0
        self.stage_latency: dict[str, list[float]] = {}
        self.enrich_budget_used: dict[str, int] = {}
        self.enrich_budget_hour = 0

    # --- helpers -------------------------------------------------------------------------

    def _lat(self, stage: str, seconds: float) -> None:
        self.stage_latency.setdefault(stage, []).append(seconds)

    def tenant(self, name: str) -> Resource:
        if name not in self.tenants:
            self.tenants[name] = Resource(self.env, f"ats_tenant:{name}", 1)
        return self.tenants[name]

    def _lognormal(self, mean: float, sigma: float = 0.4) -> float:
        return self.rng.lognormvariate(-sigma * sigma / 2, sigma) * mean

    def _day_rollover(self) -> None:
        day = int(self.env.now // 86_400)
        if day != self.day_index:
            self.day_index = day
            self.supply_left += self.sc.supply_new_per_day
        hour = int(self.env.now // 3600)
        if hour != self.enrich_budget_hour:
            self.enrich_budget_hour = hour
            self.enrich_budget_used.clear()

    def _in_human_window(self) -> bool:
        hour = (self.env.now % 86_400) / 3600
        return 9.0 <= hour < 9.0 + self.a.human_hours_per_day

    def _until_human_window(self) -> float:
        hour = (self.env.now % 86_400) / 3600
        if hour < 9.0:
            return (9.0 - hour) * 3600
        return (24.0 - hour + 9.0) * 3600

    # --- discovery -------------------------------------------------------------------------

    def discovery(self, source_name: str) -> Proc:
        s = self.a.source(source_name)
        while True:
            self._day_rollover()
            # Every leg is searched (Austin legs first, 4:1 budget share, carry-forward), so
            # the page count matches run_plan; each leg takes at most its share of listings.
            per_leg = max(1, self.a.max_results_per_source // max(1, s.legs))
            carry = 0
            for _leg in range(s.legs):
                leg_budget = per_leg + carry
                taken = 0
                for _page in range(s.pages_per_leg_used):
                    yield ("acquire", self.chrome, 0)
                    yield ("timeout", self._lognormal(s.t_search_page_s))
                    self.chrome.release()
                    self.c.search_pages += 1
                    cards = min(s.page_size, leg_budget - taken)
                    for _ in range(cards):
                        self._observe(s.name, enriched_in_search=False)
                    taken += cards
                    if taken >= leg_budget:
                        break
                carry = max(0, leg_budget - taken)
            if not self.sc.enrich_lane:
                # Today's behaviour: detail_limit detail pages inside the search run.
                for _ in range(s.detail_limit):
                    yield ("acquire", self.chrome, 0)
                    yield ("timeout", self._lognormal(s.t_detail_page_s))
                    self.chrome.release()
                    self._observe(s.name, enriched_in_search=True)
            yield ("timeout", self.a.search_interval_min * 60)

    def _observe(self, source: str, *, enriched_in_search: bool) -> None:
        self.c.observations += 1
        if self.supply_left <= 0 or self.rng.random() < self.a.p_dedupe:
            self.c.dedupe_hits += 1
            return
        fx = self.pop.one(source=source, enriched=enriched_in_search)
        key = fx.listing["provenance"][0]["employer_job_key"]
        if key and key in self.seen_keys:
            self.c.dedupe_hits += 1
            return
        self.supply_left -= 1
        self.seen_ids.add(fx.id)
        if key:
            self.seen_keys.add(key)
        self.c.unique += 1
        self.env.process(self.listing(fx))

    # --- listing lifecycle -----------------------------------------------------------------

    def listing(self, fx: Fixture) -> Proc:
        a = self.a
        t0 = self.env.now
        d = decide(fx, self.sc.strategy, a, self.judge, self.provider)
        if d.skipped_reason == "hard_constraint":
            self.c.gated_out += 1
            self.c.skip += 1
            return
        if d.deferred_to_enrichment and self.sc.enrich_lane:
            yield from self.enrich(fx)
            d = decide(fx, self.sc.strategy, a, self.judge, self.provider)
            if d.deferred_to_enrichment:
                self.c.deferred_no_text += 1
                self.c.review += 1
                return
        elif d.deferred_to_enrichment:
            self.c.deferred_no_text += 1
            self.c.review += 1
            return
        if d.attempted_calls:
            t1 = self.env.now
            yield ("acquire", self.jev, 1 if d.tier == "PREFERRED" else 0)
            self.c.jev_listings += 1
            self.c.jev_calls += d.calls
            self.c.jev_attempts += d.attempted_calls
            self.c.jev_input_bytes += d.input_bytes
            yield ("timeout", d.latency_s)
            self.jev.release()
            self._lat("semantic_fit", self.env.now - t1)
        if d.effective == APPLY:
            self.c.apply += 1
            self._lat("discover_to_apply", self.env.now - t0)
            yield from self.application(fx)
        elif d.effective == REVIEW:
            self.c.review += 1
        else:
            self.c.skip += 1

    def reserve_detail_page(self, source: str) -> Proc:
        self._day_rollover()
        while self.enrich_budget_used.get(source, 0) >= self.a.enrich_pages_per_source_per_hour:
            yield ("timeout", 60.0)
            self._day_rollover()
        self.enrich_budget_used[source] = self.enrich_budget_used.get(source, 0) + 1

    def enrich(self, fx: Fixture) -> Proc:
        a = self.a
        source = fx.listing["source"]
        s = a.source(source)
        t0 = self.env.now
        yield from self.reserve_detail_page(source)
        yield ("acquire", self.chrome, 1)
        yield ("timeout", self._lognormal(s.t_detail_page_s))
        self.chrome.release()
        self.c.enrich_pages += 1
        listing = fx.listing
        # The source's detail page: what it yields depends on the source (observed).
        if s.detail_completeness == "FULL":
            listing["description"] = _text(fx, self.rng, 3_500)
            listing["description_completeness"] = "FULL"
        elif s.detail_completeness == "PARTIAL":
            listing["description"] = _text(fx, self.rng, 300)
            listing["description_completeness"] = "PARTIAL"
        if self.rng.random() < s.p_apply_link_on_detail and fx.truth.ats_type != "generic":
            listing["application_url"] = f"https://ats.{fx.truth.tenant}.example/jobs/{fx.truth.employer_job_id}"
        if listing["description_completeness"] != "FULL" and listing.get("application_url"):
            # Employer ATS posting page: another budgeted page load.
            yield from self.reserve_detail_page(source)
            yield ("acquire", self.chrome, 1)
            yield ("timeout", self._lognormal(s.t_detail_page_s))
            self.chrome.release()
            self.c.enrich_pages += 1
            listing["description"] = _text(fx, self.rng, 3_500)
            listing["description_completeness"] = "FULL"
        if listing.get("work_arrangement") == "UNKNOWN" and listing.get("location"):
            listing["work_arrangement"] = self.rng.choice(["ONSITE", "HYBRID"])
        if listing["description_completeness"] == "FULL":
            self.c.enriched_full += 1
        self._lat("enrich", self.env.now - t0)

    # --- application ---------------------------------------------------------------------

    def application(self, fx: Fixture) -> Proc:
        a = self.a
        self.c.applications += 1
        t0 = self.env.now
        tenant = fx.truth.tenant
        lane = self.tenant(tenant)
        multistep = fx.truth.ats_type == "workday" or self.rng.random() < a.p_multistep
        service = a.t_submit_multistep_s if multistep else a.t_submit_s
        attempts = 0
        while True:
            attempts += 1
            yield ("acquire", lane, 0)
            yield ("acquire", self.browser, 1)
            yield ("timeout", self._lognormal(service))
            blocking = self._blocking_questions(fx)
            if blocking:
                self.browser.release()
                lane.release()
                self.c.needs_input += 1
                yield from self.human_input(fx, blocking)
                yield ("acquire", lane, 0)
                yield ("acquire", self.browser, 1)
                yield ("timeout", self._lognormal(a.t_resume_after_input_s))
            r = self.rng.random()
            if r < a.p_unknown:
                self.c.submission_unknown += 1
                yield ("timeout", a.tenant_spacing_s)
                self.browser.release()
                lane.release()
                yield from self.reconcile(fx, t0)
                return
            if r < a.p_unknown + a.p_retryable_failure:
                self.c.failed_retryable += 1
                self.browser.release()
                lane.release()
                if attempts >= 2:
                    return
                yield ("timeout", 600.0)
                continue
            self.c.submitted_observed += 1
            self._lat("submission", self.env.now - t0)
            yield ("timeout", a.tenant_spacing_s)
            self.browser.release()
            lane.release()
            return

    def _blocking_questions(self, fx: Fixture) -> list[str]:
        out = []
        for q in BASE_QUESTIONS[fx.truth.ats_type]:
            if q.deterministic or not q.required:
                continue
            if q.tenant_specific:
                if (fx.truth.tenant, q.key) in self.answered_tenant:
                    self.c.questions_reused += 1
                    continue
            elif q.key in self.answered_global:
                self.c.questions_reused += 1
                continue
            out.append(q.key)
        return out

    def human_input(self, fx: Fixture, questions: list[str]) -> Proc:
        a = self.a
        t0 = self.env.now
        if not self._in_human_window():
            yield ("timeout", self._until_human_window())
        yield ("acquire", self.human, 0)
        block = a.t_human_block_min * 60.0 * (0.5 + 0.5 * len(questions))
        remaining = block
        while remaining > 0:
            if not self._in_human_window():
                yield ("timeout", self._until_human_window())
            end = int(self.env.now // 86400) * 86400 + (9 + a.human_hours_per_day) * 3600
            chunk = min(remaining, end - self.env.now)
            yield ("timeout", chunk)
            self.c.human_seconds += chunk
            remaining -= chunk
        self.human.release()
        for key in questions:
            self.c.questions_asked += 1
            q = next(x for x in BASE_QUESTIONS[fx.truth.ats_type] if x.key == key)
            if q.key == "sign_in":
                self.answered_tenant.add((fx.truth.tenant, key))  # sign-in persists per tenant
            elif q.tenant_specific:
                self.answered_tenant.add((fx.truth.tenant, key))
            elif self.rng.random() < a.p_global_reuse:
                self.answered_global.add(key)
        self._lat("human_input", self.env.now - t0)

    def reconcile(self, fx: Fixture, t0: float) -> Proc:
        a = self.a
        for delay in (900.0, 3600.0, 6 * 3600.0, 24 * 3600.0):
            yield ("timeout", delay)
            yield ("acquire", self.browser, 0)
            yield ("timeout", self._lognormal(a.t_reconcile_s))
            self.browser.release()
            self.c.reconcile_checks += 1
            if self.rng.random() < 0.6:
                self.c.reconciled_submitted += 1
                self._lat("reconciliation", self.env.now - t0)
                return
        # Stays SUBMISSION_UNKNOWN: never resubmitted; the user reconciles by hand.

    # --- run ---------------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        for s in self.a.sources:
            self.env.process(self.discovery(s.name))
        horizon = self.sc.days * 86_400
        self.env.run(horizon)
        c = self.c
        days = self.sc.days
        tokens = c.jev_input_bytes / self.a.chars_per_token
        util = {
            "chrome": round(self.chrome.utilization(), 3),
            "jev": round(self.jev.utilization(), 3),
            "browser_slots": round(self.browser.utilization(), 3),
            "human_window": round(c.human_seconds / max(1.0, days * self.a.human_hours_per_day * 3600), 3),
        }
        binding = max(util, key=lambda k: util[k])
        lat = {}
        for stage, values in self.stage_latency.items():
            ordered = sorted(values)
            lat[stage] = {
                "n": len(values),
                "p50_s": round(ordered[len(ordered) // 2], 1),
                "p95_s": round(ordered[min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))], 1),
            }
        return {
            "scenario": dict(self.sc.__dict__),
            "evidence_label": "MODELED: fictional service demand, no observed site acceptance",
            "active_horizon_hours": horizon / 3600,
            "browser_service_hours": round(self.browser.busy_time / 3600, 3),
            "modeled_acceptance_yield": round((c.submitted_observed + c.reconciled_submitted) / max(1, c.applications), 4),
            "unresolved_at_horizon": c.applications - c.submitted_observed - c.reconciled_submitted,
            "screens": None,
            "interviews": None,
            "per_day": {
                "V1_observations": round(c.observations / days),
                "V2_unique_listings": round(c.unique / days),
                "dedupe_hits": round(c.dedupe_hits / days),
                "gated_out_by_code": round(c.gated_out / days),
                "enrich_page_loads": round(c.enrich_pages / days),
                "enriched_full": round(c.enriched_full / days),
                "deferred_no_text": round(c.deferred_no_text / days),
                "jev_listings": round(c.jev_listings / days),
                "jev_calls": round(c.jev_calls / days),
                "jev_attempted_calls": round(c.jev_attempts / days),
                "jev_input_tokens_est": round(tokens / days),
                "jev_usd_est": round(tokens / 1e6 * self.a.jev_usd_per_million_input_tokens / days, 4),
                "V3_effective_apply": round(c.apply / days),
                "review": round(c.review / days),
                "skip": round(c.skip / days),
                "applications_started": round(c.applications / days),
                "needs_input_stops": round(c.needs_input / days),
                "questions_asked": round(c.questions_asked / days),
                "questions_reused": round(c.questions_reused / days),
                "V5_submitted_site_observed": round(c.submitted_observed / days),
                "submission_unknown": round(c.submission_unknown / days),
                "reconciled_to_submitted": round(c.reconciled_submitted / days),
                "reconcile_checks": round(c.reconcile_checks / days),
                "failed_retryable": round(c.failed_retryable / days),
                "human_hours": round(c.human_seconds / 3600 / days, 2),
                "search_pages": round(c.search_pages / days),
            },
            "utilization": util,
            "binding_resource": binding,
            "max_queue": {
                "chrome": self.chrome.max_queue, "jev": self.jev.max_queue,
                "browser_slots": self.browser.max_queue, "human": self.human.max_queue,
            },
            "stage_latency": lat,
            "supply_left_at_end": self.supply_left,
        }


def _text(fx: Fixture, rng: random.Random, chars: int) -> str:
    from .fixtures import description_for
    return description_for(rng, fx.truth.role, chars)


def sweep(a: Assumptions, scenarios: list[Scenario]) -> list[dict[str, Any]]:
    return [Simulation(a, sc).run() for sc in scenarios]


def default_scenarios() -> list[Scenario]:
    out = []
    for supply in (50, 200, 1000, 5000):
        out.append(Scenario(supply_new_per_day=supply, enrich_lane=False, strategy="baseline_two_call",
                            label=f"today: supply {supply}, 1 browser, sequential Jev, no enrich lane"))
        out.append(Scenario(supply_new_per_day=supply, enrich_lane=True, strategy="gate_then_two_call",
                            label=f"phase1-3: supply {supply}, 1 browser, enrich lane, gate before Jev"))
        out.append(Scenario(supply_new_per_day=supply, enrich_lane=True, strategy="gate_then_two_call",
                            browser_slots=4, jev_inflight=4, chrome_cmd_parallel=2,
                            label=f"EXPERIMENTAL simulated phase4: supply {supply}, 4 browser slots, jev 4, chrome 2"))
    return out


__all__ = ["Scenario", "Simulation", "default_scenarios", "sweep"]
