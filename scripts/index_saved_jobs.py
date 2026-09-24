#!/usr/bin/env python3
"""Index the full job descriptions of resolved Saved jobs into the private RAG store.

Cover letters and complex answers need the job's actual description as job
evidence; without an indexed description the writer holds the field. This script
walks the application-URL inventory (the private export of ``application_urls``),
loads each listing from the local canonical job store, and indexes descriptions
whose completeness is FULL under the exact application URL the preparation run
will use, so retrieval scope (normalized application URL) matches at fill time.

It opens no browser, prepares nothing and never submits. Unchanged descriptions
need no new embeddings. Output is a private receipt with counts and per-row status
codes only; descriptions and credentials are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from interviewmaxxing_browser.ai.knowledge_runtime import build_knowledge_store
from interviewmaxxing_candidate.files import read_json, write_json_private
from interviewmaxxing_core import JobRecord, LocalPaths, normalize_application_url, utc_now
from interviewmaxxing_jobs.store import JobStore
from interviewmaxxing_selection.credentials import load_api_key


def _rows(inventory: Path, *, backends: set[str] | None, limit: int | None) -> list[dict[str, Any]]:
    data = read_json(inventory)
    if not isinstance(data, list):
        raise ValueError("The inventory must be a JSON list of application URL rows")
    rows = [r for r in data if isinstance(r, dict) and r.get("status") == "resolved"
            and r.get("source_application_url")]
    if backends:
        rows = [r for r in rows if r.get("backend") in backends]
    rows.sort(key=lambda r: (str(r.get("backend")), int(r.get("ordinal") or 0)))
    return rows[:limit] if limit else rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--connection-file", type=Path, required=True)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--candidate-id", default=os.environ.get("IMX_CANDIDATE_ID", "default"))
    parser.add_argument("--backends", help="comma-separated backend filter")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true", help="count without indexing")
    parser.add_argument("--receipt", type=Path, required=True, help="new private JSON receipt")
    args = parser.parse_args()
    if args.receipt.exists():
        print("error: --receipt must name a new file", file=sys.stderr)
        return 2
    paths = LocalPaths.from_env(home=args.home)
    backends = {b.strip() for b in args.backends.split(",") if b.strip()} if args.backends else None
    rows = _rows(args.inventory, backends=backends, limit=args.limit)
    jobs = JobStore(paths.home / "jobs" / "jobs.sqlite3")
    knowledge = None
    if not args.dry_run:
        knowledge = build_knowledge_store(load_api_key(env_file=args.env_file), args.connection_file)
    started = time.perf_counter()
    counts: dict[str, int] = {}
    details: list[dict[str, Any]] = []
    for row in rows:
        listing_id = str(row["listing_id"])
        url = str(row["source_application_url"])
        entry: dict[str, Any] = {"listing_id": listing_id, "backend": row.get("backend")}
        try:
            listing = jobs.get_listing(listing_id)
            if listing is None:
                status = "missing_listing"
            elif not listing.description or listing.description_completeness.value != "FULL":
                status = "description_not_full"
            else:
                normalized = normalize_application_url(url)
                if knowledge is None:
                    status = "would_index"
                else:
                    now = utc_now()
                    job = JobRecord(id=listing.id, application_url=url, normalized_url=normalized,
                                    title=listing.title, company=listing.company,
                                    location=listing.location, created_at=now, updated_at=now)
                    source_url = listing.source_url or listing.posting_url or url
                    result = knowledge.index_job(args.candidate_id, job, listing.description,
                                                 source_url)
                    entry["unchanged_source_count"] = result.get("unchanged_source_count")
                    embedding = result.get("embedding") or {}
                    entry["embedding_status"] = (embedding.get("status")
                                                 if isinstance(embedding, dict) else None)
                    entry["embedding_cost_usd"] = (embedding.get("cost_usd")
                                                   if isinstance(embedding, dict) else None)
                    status = "unchanged" if result.get("unchanged_source_count") else "indexed"
        except Exception as exc:  # provider/DB errors may carry private detail: type only
            status = f"error:{type(exc).__name__}"
        entry["status"] = status
        counts[status] = counts.get(status, 0) + 1
        details.append(entry)
    jobs.close()
    receipt = {
        "inventory": str(args.inventory), "candidate_id": args.candidate_id,
        "dry_run": args.dry_run, "rows_considered": len(rows), "counts": counts,
        "elapsed_seconds": time.perf_counter() - started,
        "known_embedding_cost_usd": sum(
            d["embedding_cost_usd"] for d in details
            if isinstance(d.get("embedding_cost_usd"), (int, float))),
        "details": details,
    }
    args.receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_json_private(args.receipt, receipt)
    print(json.dumps({k: v for k, v in receipt.items() if k != "details"}))
    return 0 if not any(k.startswith("error") for k in counts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
