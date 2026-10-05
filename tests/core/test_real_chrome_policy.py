"""Offline checks at the direct-driver boundary; no browsers or providers."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from interviewmaxxing_core.company_guard import CompanyCooldownGuard

REPO = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("real_chrome_policy", REPO / "scripts/real_chrome_policy.py")
assert spec and spec.loader
policy_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy_module)


def test_history_preserves_prior_success_when_later_attempt_failed(tmp_path):
    path = tmp_path / ".imx/dynamic-applications/real-chrome/ashby-run/ledger.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(json.dumps(row) for row in [
        {"company": "Base Power Company", "result": "submitted", "at": "2026-09-26T01:00:00"},
        {"company": "Base Power Company", "result": "validation", "at": "2026-09-26T01:01:00"},
    ]))
    guard = CompanyCooldownGuard(tmp_path / "guard.db", aliases=policy_module.ALIASES,
        clock=lambda: datetime(2026, 9, 28, tzinfo=UTC))
    assert policy_module.seed_history(guard, tmp_path, tmp_path / "home") == 1
    assert policy_module.seed_history(guard, tmp_path, tmp_path / "home") == 0
    result = guard.reserve("Base Power")
    assert not result.allowed
    assert result.blocked_until == datetime(2026, 10, 3, 6, tzinfo=UTC)


def test_pipeline_history_blocks_even_when_card_moved_on(tmp_path):
    home = tmp_path / "home"
    (home / "state").mkdir(parents=True)
    with sqlite3.connect(home / "state/pipeline.sqlite3") as db:
        db.execute("CREATE TABLE items (id, candidate_id, body)")
        db.execute("CREATE TABLE history (sequence, candidate_id, body)")
        card = {"id": "p1", "lane": "interviewing", "tracking": {"company": "Example"}}
        db.execute("INSERT INTO items VALUES (?, ?, ?)", ("p1", "default", json.dumps(card)))
        event = {"item_id": "p1", "to_lane": "applied", "changed_at": "2026-09-26T12:00:00Z"}
        db.execute("INSERT INTO history VALUES (?, ?, ?)", (1, "default", json.dumps(event)))
    guard = CompanyCooldownGuard(tmp_path / "guard.db", clock=lambda: datetime(2026, 9, 28, tzinfo=UTC))
    assert policy_module.seed_history(guard, tmp_path, home) == 1
    assert not guard.reserve("Example").allowed


@pytest.mark.parametrize("outcome,blocked", [
    ("submitted", True), ("submitted_unconfirmed", True), ("needs_code", True),
    ("send_failed", True), ("validation", False), ("rejected_spam", False),
    ("needs_answers", False), ("dry_filled", False), ("closed_posting", False),
])
def test_fill_boundary_retains_possible_dispatches(tmp_path, monkeypatch, outcome, blocked):
    guard = CompanyCooldownGuard(tmp_path / "guard.db")
    monkeypatch.setattr(policy_module, "policy", lambda: guard)
    calls = []

    @policy_module.guarded_fill
    def fill(chrome, job, listing_file, dry):
        calls.append(job)
        return {"result": outcome}

    fill(None, {"company": "Example"}, None, False)
    assert guard.reserve("Example").allowed is not blocked
    if blocked:
        assert fill(None, {"company": "Example"}, None, False)["result"] == "company_cooldown"
        assert len(calls) == 1


def test_driver_exception_keeps_reservation(tmp_path, monkeypatch):
    guard = CompanyCooldownGuard(tmp_path / "guard.db")
    monkeypatch.setattr(policy_module, "policy", lambda: guard)

    @policy_module.guarded_fill
    def fill(chrome, *args):
        chrome.cli("click", "--name", "Submit Application")

    class Chrome:
        def cli(self, *args):
            raise TimeoutError("Unknown whether click dispatched")

    with pytest.raises(TimeoutError):
        fill(Chrome(), {"company": "Example"}, None, False)
    assert not guard.reserve("Example").allowed


def test_exception_before_submit_releases_company(tmp_path, monkeypatch):
    guard = CompanyCooldownGuard(tmp_path / "guard.db")
    monkeypatch.setattr(policy_module, "policy", lambda: guard)

    @policy_module.guarded_fill
    def fill(*args):
        raise TimeoutError("RAG timed out before a click")

    with pytest.raises(TimeoutError):
        fill(None, {"company": "Example"}, None, False)
    assert guard.reserve("Example").allowed


@pytest.mark.parametrize("application_url", [
    "https://job-boards.greenhouse.io/example/jobs/123",
    "https://job-boards.greenhouse.io/embed/job_app?for=example&token=123",
])
@pytest.mark.parametrize("confirmation_url", [
    "https://job-boards.greenhouse.io/example/jobs/123/confirmation",
    "https://job-boards.greenhouse.io/embed/job_app/confirmation?for=example&token=123",
])
def test_greenhouse_receipt_completes_same_job_only(tmp_path, application_url, confirmation_url):
    guard = CompanyCooldownGuard(tmp_path / "guard.db")
    reservation = guard.reserve("Example")
    pending = tmp_path / "pending-code.json"
    url = "https://job-boards.greenhouse.io/example/jobs/123"
    pending.write_text(json.dumps({"company": "Example", "url": application_url,
        "company_reservation": reservation.token, "result": "needs_code"}))
    receipt = {"url": confirmation_url, "text": "Thank you for applying"}
    with pytest.raises(ValueError):
        policy_module.finish_greenhouse_code(pending, {**receipt, "url": url.replace("123", "456") + "/confirmation"}, guard=guard)
    assert pending.exists()
    result = policy_module.finish_greenhouse_code(pending, receipt, guard=guard)
    assert result["result"] == "submitted"
    assert not pending.exists()
    assert json.loads((tmp_path / "ledger.jsonl").read_text())["result"] == "submitted"
    assert guard.reserve("Example").reason == "company_cooldown"
