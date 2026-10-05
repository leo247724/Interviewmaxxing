"""Shared company guard for the local, personal-Chrome application drivers.

Importing this module does not open a browser or submit anything. Historical
ledgers and pipeline history are read-only; only the separate cooldown DB changes.
"""
from __future__ import annotations
import os

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from interviewmaxxing_core.company_guard import CompanyCooldownGuard
from interviewmaxxing_core.greenhouse_email import greenhouse_job_identity

REPO = Path(__file__).resolve().parents[1]
ALIASES = {"Base Power": "Base Power Company", "Jane App": "Jane"}
ACCEPTED = {"submitted", "submitted_unconfirmed", "submitted_wrong_answer", "already_applied"}
NO_ACCEPTANCE = {
    "needs_answers", "dry_filled", "skipped", "closed_posting", "no_form",
    "validation", "rejected_spam",
}


def historical_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # The September direct drivers used local time.strftime on this Chicago host.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ZoneInfo("America/Chicago"))


def seed_history(guard: CompanyCooldownGuard, repo: Path, home: Path) -> int:
    count = 0
    root = repo / ".imx/dynamic-applications"
    for relative in (
        "real-chrome/ashby-run/ledger.jsonl", "real-chrome/gh-run/ledger.jsonl",
        "real-chrome/ledger.jsonl", "wellfound-run/ledger.jsonl",
    ):
        path = root / relative
        if not path.exists():
            continue
        for line_number, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            row = json.loads(line)  # Corrupt history must not silently permit a duplicate.
            if row.get("result") in ACCEPTED:
                count += guard.record_history(
                    row["company"], historical_time(row.get("at")),
                    f"ledger:{relative}:{line_number}",
                )
    pipeline = home / "state/pipeline.sqlite3"
    if pipeline.exists():
        with sqlite3.connect(f"file:{pipeline}?mode=ro", uri=True) as db:
            cards = {r[0]: json.loads(r[1]) for r in db.execute(
                "SELECT id, body FROM items WHERE candidate_id=?", (guard.profile_id,),
            )}
            seen = set()
            for sequence, raw in db.execute(
                "SELECT sequence, body FROM history WHERE candidate_id=? ORDER BY sequence",
                (guard.profile_id,),
            ):
                row = json.loads(raw)
                if row.get("to_lane") != "applied":
                    continue
                card = cards.get(row["item_id"])
                if card is None:
                    raise ValueError("Applied history has no company-bearing card")
                seen.add(row["item_id"])
                count += guard.record_history(
                    card["tracking"]["company"], historical_time(row.get("changed_at")),
                    f"pipeline:{sequence}",
                )
            for card in cards.values():
                if card["lane"] == "applied" and card["id"] not in seen:
                    count += guard.record_history(
                        card["tracking"]["company"], None, f"pipeline-undated:{card['id']}",
                    )
    return count


def policy() -> CompanyCooldownGuard:
    home = Path.home() / ".interviewmaxxing"
    # The person's rule (2026-09-29, 14:50): one role per company per week.
    from datetime import timedelta
    guard = CompanyCooldownGuard(home / "state/company-cooldown.sqlite3", aliases=ALIASES, cooldown=timedelta(days=7))
    seed_history(guard, REPO, home)
    return guard


def guarded_fill(function: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Reserve before any fill/provider work; unknown outcomes remain reserved."""
    @wraps(function)
    def wrapped(chrome: Any, job: dict[str, Any], listing_file: Any, dry: bool) -> dict[str, Any]:
        guard = policy()
        reservation = guard.reserve(job["company"])
        if not reservation.allowed:
            return {
                **{k: job.get(k) for k in ("listing_id", "company", "title", "url")},
                "result": "company_cooldown", "reason": reservation.reason,
                "blocked_until": reservation.blocked_until.isoformat() if reservation.blocked_until else None,
                "cost": 0.0,
            }
        assert reservation.token
        # goal 3: answer rules that depend on the employer ("previously applied to <company>?") read the current job here
        os.environ["IMX_CURRENT_COMPANY"] = str(job.get("company") or "")
        os.environ["IMX_CURRENT_LISTING"] = str(job.get("listing_id") or "")
        class DispatchTracker:
            dispatched = False

            def __getattr__(self, name: str) -> Any:
                return getattr(chrome, name)

            def cli(self, *args: str, **kwargs: Any) -> Any:
                if args and args[0] == "click" and any(
                    arg.lower() == "submit application" for arg in args[1:]
                ):
                    self.dispatched = True
                return chrome.cli(*args, **kwargs)

        tracked = DispatchTracker()
        try:
            entry = function(tracked, job, listing_file, dry)
        except Exception as exc:
            if not tracked.dispatched:
                guard.release(reservation.token)
            else:
                # Let callers persist the token alongside an uncertain result.
                exc.company_reservation = reservation.token  # type: ignore[attr-defined]
            raise
        entry["company_reservation"] = reservation.token
        if entry.get("result") in ACCEPTED:
            guard.commit(reservation.token)
        elif entry.get("result") in NO_ACCEPTANCE:
            guard.release(reservation.token)
        return entry
    return wrapped


def finish_greenhouse_code(
    pending_path: Path, receipt: dict[str, str], *, guard: CompanyCooldownGuard | None = None,
) -> dict[str, Any]:
    """Record already-observed browser confirmation; never enter a code or submit.

    The supervising agent supplies a fresh OpenCLI URL/text readback from the
    retained session after authorized code entry. Never pass the email code here.
    """
    pending = json.loads(pending_path.read_text())
    expected, observed = urlsplit(pending["url"]), urlsplit(receipt["url"])
    identity = greenhouse_job_identity(pending["url"])
    same_job = (
        greenhouse_job_identity(receipt["url"]) == identity
        if identity else observed.hostname == expected.hostname
        and observed.path.rstrip("/") == expected.path.rstrip("/") + "/confirmation"
    )
    if (
        observed.scheme != "https" or not same_job
        or not observed.path.rstrip("/").endswith("/confirmation")
        or not re.search(r"thank you for applying|application has been received", receipt["text"], re.I)
    ):
        raise ValueError("Receipt is not confirmation for this pending Greenhouse job")
    guard = guard or policy()
    token = pending["company_reservation"]
    with sqlite3.connect(guard.db_path) as db:
        row = db.execute(
            "SELECT status FROM company_cooldown WHERE token=? AND profile=?",
            (token, guard.profile_id),
        ).fetchone()
    if row is None:
        raise ValueError("Pending company reservation was not found")
    entry = {**pending, "result": "submitted", "verified": receipt["url"], "after": receipt["text"][:300]}
    guard.commit(token)
    ledger = pending_path.parent / "ledger.jsonl"
    already_recorded = any(
        json.loads(line).get("company_reservation") == token
        and json.loads(line).get("result") == "submitted"
        for line in ledger.read_text().splitlines() if line.strip()
    ) if ledger.exists() else False
    if not already_recorded:
        with ledger.open("a") as out:
            out.write(json.dumps(entry) + "\n")
    if pending.get("pipeline_id"):
        from interviewmaxxing_pipeline import PipelineStore
        with PipelineStore.open(Path.home() / ".interviewmaxxing/state/pipeline.sqlite3") as cards:
            card = cards.get_item(guard.profile_id, pending["pipeline_id"])
            if card.lane != "applied":
                cards.move_item(guard.profile_id, card.id, "applied", expected_revision=card.revision,
                                note=f"Greenhouse application confirmed: {receipt['url']}")
    pending_path.unlink()
    return entry


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--finish-greenhouse", type=Path, metavar="RECEIPT_JSON")
    args = parser.parse_args()
    if args.finish_greenhouse:
        result = finish_greenhouse_code(
            REPO / ".imx/dynamic-applications/real-chrome/gh-run/pending-code.json",
            json.loads(args.finish_greenhouse.read_text()),
        )
        print(json.dumps({"result": result["result"], "company": result["company"]}))
        raise SystemExit(0)
    guard = policy()
    with sqlite3.connect(guard.db_path) as db:
        rows = db.execute(
            "SELECT status, count(*), count(distinct company) FROM company_cooldown "
            "WHERE profile=? GROUP BY status", (guard.profile_id,),
        ).fetchall()
    print(json.dumps({"database": str(guard.db_path), "history": rows}))
