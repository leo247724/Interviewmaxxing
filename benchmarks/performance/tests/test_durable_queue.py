import tempfile
import unittest
from pathlib import Path

from imx_perf.durable_queue import BudgetExhausted, DurableQueue, LeaseLost


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t


class DurableQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.q = DurableQueue(Path(self.tmp.name) / "q.sqlite3", clock=self.clock)

    def tearDown(self) -> None:
        self.q.close()
        self.tmp.cleanup()

    def test_enqueue_is_idempotent_while_active(self) -> None:
        a, created_a = self.q.enqueue("apply", "app_1", lanes=["browser_profile:slot-0"])
        b, created_b = self.q.enqueue("apply", "app_1", lanes=["browser_profile:slot-0"])
        self.assertTrue(created_a)
        self.assertFalse(created_b)
        self.assertEqual(a.id, b.id)

    def test_claim_is_exclusive_and_lane_bounded(self) -> None:
        self.q.set_lane("browser_profile:slot-0", max_active=1)
        self.q.enqueue("apply", "app_1", lanes=["browser_profile:slot-0"], priority=1)
        self.q.enqueue("apply", "app_2", lanes=["browser_profile:slot-0"], priority=5)
        first = self.q.claim_next("w1", ttl_s=10)
        self.assertIsNotNone(first)
        item, lease = first  # type: ignore[misc]
        self.assertEqual(item.key, "app_2", "highest priority first")
        self.assertIsNone(self.q.claim_next("w2", ttl_s=10), "lane is full")
        self.q.complete(lease, {"ok": True})
        second = self.q.claim_next("w2", ttl_s=10)
        self.assertIsNotNone(second)
        self.assertEqual(second[0].key, "app_1")  # type: ignore[index]

    def test_expired_lease_is_recovered_and_old_token_rejected(self) -> None:
        self.q.enqueue("apply", "app_1")
        item, lease = self.q.claim_next("w1", ttl_s=5)  # type: ignore[misc]
        self.clock.t += 6
        recovered = self.q.recover_expired()
        self.assertEqual(recovered, [item.id])
        with self.assertRaises(LeaseLost):
            self.q.complete(lease)
        again = self.q.claim_next("w2", ttl_s=5)
        self.assertIsNotNone(again)
        self.assertEqual(again[0].attempts, 2)  # type: ignore[index]

    def test_renew_extends_and_effect_is_once(self) -> None:
        self.q.enqueue("apply", "app_1")
        item, lease = self.q.claim_next("w1", ttl_s=5)  # type: ignore[misc]
        self.clock.t += 4
        lease = self.q.renew(lease, ttl_s=5)
        self.clock.t += 4
        self.assertTrue(self.q.apply_effect(lease, "submit:app_1"))
        self.assertFalse(self.q.apply_effect(lease, "submit:app_1"))
        self.q.complete(lease)
        self.assertEqual(self.q.stats()["effects"], 1)

    def test_fail_retries_then_dead_letters(self) -> None:
        self.q.enqueue("decide", "lst_1", max_attempts=2)
        _, lease = self.q.claim_next("w1", ttl_s=5)  # type: ignore[misc]
        item = self.q.fail(lease, "transient", retry_in_s=30)
        self.assertEqual(item.state, "READY")
        self.assertIsNone(self.q.claim_next("w1", ttl_s=5), "not available before retry_in_s")
        self.clock.t += 31
        _, lease = self.q.claim_next("w1", ttl_s=5)  # type: ignore[misc]
        item = self.q.fail(lease, "still failing", retry_in_s=30)
        self.assertEqual(item.state, "DEAD")

    def test_backpressure_and_budget(self) -> None:
        self.q.set_lane("enrich", max_active=1, high_water=2)
        self.assertTrue(self.q.admit("enrich"))
        self.q.enqueue("enrich", "a", lanes=["enrich"])
        self.q.enqueue("enrich", "b", lanes=["enrich"])
        self.assertFalse(self.q.admit("enrich"))
        self.q.set_budget("jev_usd", cap=1.0)
        self.q.spend("jev_usd", 0.7)
        with self.assertRaises(BudgetExhausted):
            self.q.spend("jev_usd", 0.5)

    def test_release_does_not_count_an_attempt(self) -> None:
        self.q.enqueue("apply", "app_1")
        _, lease = self.q.claim_next("w1", ttl_s=5)  # type: ignore[misc]
        self.q.release(lease)
        item, _ = self.q.claim_next("w1", ttl_s=5)  # type: ignore[misc]
        self.assertEqual(item.attempts, 1)

    def test_spacing_between_completions_on_a_lane(self) -> None:
        self.q.set_lane("ats_tenant:x", max_active=1, spacing_s=120)
        self.q.enqueue("apply", "a", lanes=["ats_tenant:x"])
        self.q.enqueue("apply", "b", lanes=["ats_tenant:x"])
        _, lease = self.q.claim_next("w", ttl_s=5)  # type: ignore[misc]
        self.q.complete(lease)
        self.assertIsNone(self.q.claim_next("w", ttl_s=5), "spacing not elapsed")
        self.clock.t += 121
        self.assertIsNotNone(self.q.claim_next("w", ttl_s=5))


if __name__ == "__main__":
    unittest.main()

class SafetyRegressionTests(unittest.TestCase):
    setUp = DurableQueueTests.setUp
    tearDown = DurableQueueTests.tearDown
    def test_full_decision_identity(self):
        from imx_perf.durable_queue import decision_key
        parts = dict(candidate_id='candidate-a', candidate_evidence_version='facts-1', job_id='job-a',
                     job_evidence_version='evidence-1', preferences_version='prefs-1', rubric_version='r1',
                     model_version='m1')
        base = decision_key(**parts)
        for name in parts:
            changed = {**parts, name: parts[name] + '-changed'}
            self.assertNotEqual(base, decision_key(**changed), name)

    def test_crash_before_effect_routes_to_reconciliation(self):
        item, _ = self.q.enqueue('apply', 'app_1', max_attempts=1)
        _, lease = self.q.claim_next('old', ttl_s=5)
        self.q.begin_submission(lease)
        self.clock.t += 6
        self.q.recover_expired()
        recovered, new = self.q.claim_next('new', ttl_s=5)
        self.assertEqual(recovered.id, item.id)
        self.assertEqual(recovered.kind, 'reconcile')
        self.assertFalse(self.q.effect_applied('submit:app_1'))
        coalesced, created = self.q.enqueue('apply', 'app_1')
        self.assertFalse(created)
        self.assertEqual(coalesced.kind, 'reconcile')
        from imx_perf.durable_queue import QueueError
        with self.assertRaises(QueueError):
            self.q.begin_submission(new)
        with self.assertRaises(LeaseLost):
            self.q.release(lease)
        self.q.complete(new, {'outcome': 'unknown; manual reconciliation'})

    def test_reconciliation_cannot_starve_under_priority_arrivals(self):
        self.q.enqueue('reconcile', 'unknown', priority=-999)
        self.clock.t += 901
        for i in range(10):
            self.q.enqueue('apply', f'new-{i}', priority=999)
        item, _ = self.q.claim_next('worker', ttl_s=5)
        self.assertEqual(item.kind, 'reconcile')

    def test_failed_attempts_reserve_budget_and_cooldown_is_shared(self):
        from imx_perf.durable_queue import QueueError
        self.q.set_budget('provider', cap=1)
        self.q.reserve_provider_attempt('provider', 'call-1', .6)
        self.q.settle_provider_attempt('call-1', outcome='timeout')
        other = DurableQueue(self.q.path, clock=self.clock)
        try:
            with self.assertRaises(BudgetExhausted):
                other.reserve_provider_attempt('provider', 'retry-1', .6)
            self.q.cooldown('provider', 3600)
            other.cooldown('provider', 10)
            self.clock.t += 11
            with self.assertRaises(QueueError):
                other.reserve_provider_attempt('provider', 'call-2', .2)
            self.clock.t += 3600
            other.reserve_provider_attempt('provider', 'call-2', .2)
            with self.assertRaises(QueueError):
                other.reserve_provider_attempt('provider', 'call-2', .2)
            other.settle_provider_attempt('call-2', outcome='success', actual_cost=.1)
            self.assertAlmostEqual(other._conn.execute("SELECT spent FROM budgets WHERE name='provider'").fetchone()[0], .7)
        finally:
            other.close()

    def test_owner_and_expiry_fence_every_release(self):
        from dataclasses import replace
        self.q.enqueue('decide', 'a')
        _, lease = self.q.claim_next('worker', ttl_s=5)
        with self.assertRaises(LeaseLost):
            self.q.release(replace(lease, owner='impostor'))
        self.clock.t += 6
        with self.assertRaises(LeaseLost):
            self.q.release(lease)
