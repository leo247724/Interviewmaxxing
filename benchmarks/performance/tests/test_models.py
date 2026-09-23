import unittest

from imx_perf import chaos, funnel, pipeline_sim, selection_sim
from imx_perf.config import Assumptions
from imx_perf.fixtures import generate, listing_id_for


class FunnelTests(unittest.TestCase):
    def test_astra_arithmetic(self) -> None:
        a = Assumptions()
        t = funnel.tier_requirements(a, 1000)
        lane = next(x for x in t["lanes"] if x["window_h"] == 24.0 and x["service_s"] == 60.0)
        self.assertAlmostEqual(lane["per_minute"], 0.694, places=3)
        self.assertAlmostEqual(lane["completion_budget_s"], 86.4, places=2)
        t8 = next(x for x in t["lanes"] if x["window_h"] == 8.0 and x["service_s"] == 60.0)
        self.assertAlmostEqual(t8["completion_budget_s"], 28.8, places=2)

    def test_tiers_scale_linearly(self) -> None:
        a = Assumptions()
        one = funnel.tier_requirements(a, 1000)
        ten = funnel.tier_requirements(a, 10000)
        self.assertAlmostEqual(ten["enriched_listings_per_day"] / one["enriched_listings_per_day"], 10, delta=0.05)

    def test_run_plan_counts_full_descriptions_from_detail_pages_only(self) -> None:
        plan = funnel.run_plan(Assumptions())
        self.assertEqual(plan["full_description_listings"], 20)
        self.assertEqual(plan["max_observations"], 200)


class FixtureTests(unittest.TestCase):
    def test_population_is_deterministic_and_ids_follow_rule(self) -> None:
        a = Assumptions()
        x = generate(a, 50, seed=1)
        y = generate(a, 50, seed=1)
        self.assertEqual([f.id for f in x], [f.id for f in y])
        for f in x:
            self.assertEqual(f.id, listing_id_for(f.listing["source"], f.listing["source_listing_id"]))
            has_text = bool((f.listing["description"] or "").strip())
            self.assertEqual(has_text, f.listing["description_completeness"] != "NONE")
            self.assertTrue(f.listing["provenance"][0]["source"] == f.listing["source"])
            for url in (f.listing["posting_url"], f.listing["source_url"], f.listing.get("application_url") or ""):
                self.assertNotIn(".com", url, "fixture URLs must be fictional .example domains")


class SelectionTests(unittest.TestCase):
    def test_gated_strategies_never_call_for_incurable_or_curable_holds(self) -> None:
        a = Assumptions()
        fixtures = generate(a, 300, seed=3)
        out = selection_sim.run_strategies(fixtures, a, seed=3)
        by = {s["strategy"]: s for s in out["strategies"]}
        self.assertGreater(by["baseline_two_call"]["calls"], by["gate_then_two_call"]["calls"])
        self.assertEqual(by["gate_then_two_call"]["calls"], 2 * by["gate_then_two_call"]["reached_jev"])
        self.assertLessEqual(by["gate_combined_selective"]["calls"], by["gate_then_two_call"]["calls"])
        self.assertEqual(by["gate_single_request"]["calls"], by["gate_single_request"]["reached_jev"])
        self.assertLess(by["gate_compact_final"]["input_tokens"], by["gate_then_two_call"]["input_tokens"])

    def test_effective_apply_requires_no_holds(self) -> None:
        self.assertEqual(selection_sim.effective_decision("APPLY", ["LOW_CONFIDENCE"]), "REVIEW")
        self.assertEqual(selection_sim.effective_decision("SKIP", ["LISTING_CLOSED"]), "SKIP")
        self.assertEqual(selection_sim.effective_decision(None, []), "REVIEW")
        self.assertEqual(selection_sim.effective_decision("SKIP", ["LOW_CONFIDENCE"]), "REVIEW")

    def test_cache_scenarios_show_application_url_invalidation(self) -> None:
        a = Assumptions()
        fixtures = generate(a, 300, seed=5)
        cache = selection_sim.cache_scenarios(fixtures, a, seed=5)
        enrich = cache["enrichment_adds_application_url"]
        self.assertEqual(enrich["misses_current_rule"], enrich["affected"])
        self.assertEqual(enrich["hits_proposed_rule"], enrich["affected"])
        reobs = cache["reobservation_posted_text_only"]
        self.assertEqual(reobs["cache_hits"], reobs["of"])


class PipelineTests(unittest.TestCase):
    def test_simulation_conserves_listings(self) -> None:
        a = Assumptions()
        sc = pipeline_sim.Scenario(supply_new_per_day=100, days=0.5, seed=9)
        r = pipeline_sim.Simulation(a, sc).run()
        d = r["per_day"]
        self.assertGreater(d["V1_observations"], 0)
        self.assertLessEqual(d["V5_submitted_site_observed"], d["applications_started"])
        self.assertIn(r["binding_resource"], ("chrome", "jev", "browser_slots", "human_window"))


class ChaosTests(unittest.TestCase):
    def test_small_chaos_run_passes(self) -> None:
        cfg = chaos.ChaosConfig(items=30, workers=3, seed=2, max_wall_s=15.0)
        result = chaos.run(cfg)
        self.assertTrue(result.passed, result.failures)
        self.assertEqual(result.effects + result.reconciled_without_effect, 30)
        self.assertEqual(result.double_effects, 0)


if __name__ == "__main__":
    unittest.main()

class RegressionTests(unittest.TestCase):
    def test_independent_final_cannot_see_sibling_answers(self):
        fx = generate(Assumptions(), 1, seed=19)[0]
        first = selection_sim.StubJudge(42).final(fx, {}, sees_assessments=False)
        second = selection_sim.StubJudge(42).final(
            fx, {'fake_sibling': selection_sim.Answer('contradiction', .99)}, sees_assessments=False)
        self.assertEqual(first, second)

    def test_all_failed_provider_attempts_are_still_counted(self):
        a = Assumptions(p_transient_provider_failure=1)
        result = selection_sim.run_strategies(generate(a, 100, seed=5), a, seed=5)
        baseline = result['strategies'][0]
        self.assertEqual(baseline['calls'], 0)
        self.assertGreater(baseline['attempted_calls'], 0)
        self.assertGreater(baseline['input_tokens'], 0)
        self.assertGreater(baseline['mean_latency_s'], 0)

    def test_permanent_modeled_retry_failure_never_becomes_acceptance(self):
        a = Assumptions(p_unknown=0, p_retryable_failure=.999999, t_submit_s=1,
                        t_submit_multistep_s=1)
        sim = pipeline_sim.Simulation(a, pipeline_sim.Scenario(seed=9))
        sim._blocking_questions = lambda _fx: []
        fx = generate(a, 1, seed=9)[0]
        sim.env.process(sim.application(fx))
        sim.env.run(10000)
        self.assertEqual(sim.c.failed_retryable, 2)
        self.assertEqual(sim.c.submitted_observed, 0)

    def test_invalid_model_denominators_are_rejected(self):
        for override in ({'p_dedupe': 1}, {'utilization_target': 0}, {'browser_slots': 0},
                         {'p_unknown': .5, 'p_retryable_failure': .5}):
            with self.assertRaises(ValueError):
                Assumptions(**override)
