"""Behavioral checks for cross-lane, cross-process company application exclusion."""

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from interviewmaxxing_core.company_guard import CompanyCooldownGuard, normalize_company

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def test_independent_processes_reserve_only_once(tmp_path: Path) -> None:
    # Include concurrent schema creation, not just calls on an initialized connection.
    path = str(tmp_path / "shared.sqlite3")
    script = (
        "import sys; from interviewmaxxing_core.company_guard import CompanyCooldownGuard; "
        "print(int(CompanyCooldownGuard(sys.argv[1]).reserve('Base Power, Inc.').allowed))"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(8)
    ]
    results = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        results.append(int(stdout.strip()))
    assert sum(results) == 1


def test_rolling_boundary_and_commit_idempotence(tmp_path: Path) -> None:
    now = NOW
    guard = CompanyCooldownGuard(tmp_path / "db", clock=lambda: now)
    reservation = guard.reserve("Base Power")
    assert reservation.allowed and reservation.token
    assert guard.commit(reservation.token)
    now += timedelta(days=6, hours=23, minutes=59, seconds=59)
    assert not guard.commit(reservation.token)
    blocked = guard.reserve("Base Power")
    assert not blocked.allowed
    assert blocked.blocked_until == NOW + timedelta(days=7)
    assert not guard.release(reservation.token)
    now += timedelta(seconds=1)
    assert guard.reserve("Base Power").allowed


def test_pending_never_expires_and_only_explicit_release_reopens(tmp_path: Path) -> None:
    path = tmp_path / "db"
    first = CompanyCooldownGuard(path, clock=lambda: NOW).reserve("Example")
    later = CompanyCooldownGuard(path, clock=lambda: NOW + timedelta(days=365))
    blocked = later.reserve("Example")
    assert blocked.reason == "pending_or_ambiguous_dispatch"
    assert blocked.blocked_until is None
    assert first.token
    assert later.release(first.token)
    assert not later.release(first.token)
    assert later.reserve("Example").allowed


def test_profile_isolation_and_token_ownership(tmp_path: Path) -> None:
    one = CompanyCooldownGuard(tmp_path / "db", profile_id="one")
    two = CompanyCooldownGuard(tmp_path / "db", profile_id="two")
    reservation = one.reserve("Example")
    assert reservation.token
    assert not two.release(reservation.token)
    assert not two.commit(reservation.token)
    assert two.reserve("Example").allowed
    assert not one.reserve("Example").allowed


def test_alias_persists_and_includes_existing_history(tmp_path: Path) -> None:
    path = tmp_path / "db"
    old = CompanyCooldownGuard(path, clock=lambda: NOW)
    old.record_history("BasePower", NOW, "batch:1")
    CompanyCooldownGuard(path, aliases={"BasePower": "Base Power"})
    other_process = CompanyCooldownGuard(path, clock=lambda: NOW)
    assert not other_process.reserve("BASE POWER, Inc.").allowed
    assert not other_process.reserve("basepower").allowed
    assert other_process.reserve("Base Power Systems").allowed


def test_alias_chains_resolve_across_reopens(tmp_path: Path) -> None:
    path = tmp_path / "db"
    guard = CompanyCooldownGuard(path, aliases={"A": "B", "B": "C"})
    assert guard.reserve("A").allowed
    assert not CompanyCooldownGuard(path).reserve("C").allowed
    assert not CompanyCooldownGuard(path).reserve("B").allowed
    with pytest.raises(ValueError, match="Conflicting"):
        CompanyCooldownGuard(path, aliases={"A": "Other"})


def test_historical_import_is_idempotent_and_uses_latest_date(tmp_path: Path) -> None:
    guard = CompanyCooldownGuard(tmp_path / "db", clock=lambda: NOW)
    assert guard.record_history("Example", NOW - timedelta(days=9), "batch:old")
    assert not guard.record_history("Example", NOW, "batch:old")
    assert guard.record_history("Example", NOW.isoformat(), "pipeline:new")
    assert guard.reserve("Example").blocked_until == NOW + timedelta(days=7)
    assert guard.record_history("Different", NOW - timedelta(days=7), "batch:other")
    assert guard.reserve("Different").allowed


def test_unknown_date_cannot_silently_age_out_or_be_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "db"
    guard = CompanyCooldownGuard(path, clock=lambda: NOW)
    assert guard.record_history("Example", None, "pipeline:unknown")
    assert not guard.record_history("Example", NOW - timedelta(days=30), "pipeline:unknown")
    later = CompanyCooldownGuard(path, clock=lambda: NOW + timedelta(days=365))
    blocked = later.reserve("Example")
    assert blocked.reason == "unknown_historical_date"
    assert blocked.blocked_until is None


def test_normalization_is_conservative() -> None:
    assert normalize_company("  ＢＡＳＥ Power, Inc. ") == "base power"  # noqa: RUF001
    assert normalize_company("Base Power LLC") == "base power"
    assert normalize_company("Power Group") != normalize_company("Power")
    assert normalize_company("The Company") != normalize_company("The")
    assert normalize_company("Inc") == "inc"
    with pytest.raises(ValueError):
        normalize_company("---")


def test_rejects_naive_dates_and_empty_source(tmp_path: Path) -> None:
    guard = CompanyCooldownGuard(tmp_path / "db")
    with pytest.raises(ValueError, match="timezone"):
        guard.record_history("Example", datetime(2026, 9, 28), "source:1")
    with pytest.raises(ValueError, match="source_id"):
        guard.record_history("Example", NOW, " ")
