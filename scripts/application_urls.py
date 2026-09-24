#!/usr/bin/env python3
"""Small, assignment-scoped Supabase client. Resolving/classifying URLs is worker work.

Requires psycopg[binary]. Credentials are read from a private JSON file, never argv values.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row


def validate_rows(rows: object) -> list[dict]:
    if not isinstance(rows, list) or not rows or len(rows) > 100:
        raise ValueError("Expected an array containing 1 to 100 results")
    seen = set()
    allowed = {"listing_id", "source_application_url", "backend", "status", "evidence_url", "notes"}
    for row in rows:
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError("Unexpected result fields")
        ident = row.get("listing_id")
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ValueError("Missing or repeated listing_id")
        seen.add(ident)
        if row.get("status") not in {"resolved", "blocked", "closed", "ambiguous"}:
            raise ValueError("Invalid status")
        for key in ("source_application_url", "evidence_url"):
            value = row.get(key)
            if value is not None:
                parsed = urlsplit(value)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
                    raise ValueError(f"Invalid {key}")
        if row["status"] == "resolved" and (
            not row.get("source_application_url") or not row.get("evidence_url")
            or row.get("backend") in {None, "", "unknown"}
        ):
            raise ValueError("A resolved mapping needs a URL, backend and evidence URL")
        if row["status"] != "resolved" and not row.get("notes"):
            raise ValueError("An unresolved mapping needs an explanation")
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--pending", action="store_true")
    listing.add_argument("--output", type=Path)
    save = commands.add_parser("save")
    save.add_argument("--file", type=Path, required=True)
    commands.add_parser("status")
    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("--status", default="working")
    heartbeat.add_argument("--model", default="claude-opus-5-5")
    args = parser.parse_args()
    cfg = json.loads(args.credentials.read_text())
    scope = (cfg["run_id"], cfg["worker_id"])
    rows = validate_rows(json.loads(args.file.read_text())) if args.command == "save" else None
    with psycopg.connect(**cfg["connection"], row_factory=dict_row) as conn:
        conn.execute("set statement_timeout = '20s'")
        if args.command == "list":
            sql = "select * from public.application_urls where run_id=%s and worker_id=%s"
            if args.pending:
                sql += " and status='pending'"
            data = conn.execute(sql + " order by ordinal", scope).fetchall()
            if args.output:
                args.output.write_text(json.dumps(data, default=str, indent=2))
                print(json.dumps({"rows": len(data), "output": str(args.output)}))
            else:
                print(json.dumps(data, default=str))
        elif args.command == "save":
            checked = datetime.now(UTC)
            for row in rows:
                cursor = conn.execute(
                    """update public.application_urls set source_application_url=%s,backend=%s,
                    status=%s,evidence_url=%s,notes=%s,checked_at=%s
                    where run_id=%s and worker_id=%s and listing_id=%s""",
                    (row.get("source_application_url"), row.get("backend", "unknown"),
                     row["status"], row.get("evidence_url"), row.get("notes", ""), checked,
                     *scope, row["listing_id"]),
                )
                if cursor.rowcount != 1:
                    raise ValueError("Result does not belong to this worker; batch rolled back")
            conn.execute("update public.application_url_workers set heartbeat_at=now() where run_id=%s and worker_id=%s", scope)
            conn.commit()
            print(json.dumps({"saved": len(rows), "worker": cfg["worker_id"]}))
        elif args.command == "heartbeat":
            conn.execute("update public.application_url_workers set status=%s,model=%s,heartbeat_at=now() where run_id=%s and worker_id=%s",
                         (args.status, args.model, *scope))
            print(json.dumps({"worker": cfg["worker_id"], "status": args.status}))
        else:
            data = conn.execute("select status,count(*) as count from public.application_urls where run_id=%s and worker_id=%s group by status", scope).fetchall()
            print(json.dumps({"worker": cfg["worker_id"], "counts": data}))


if __name__ == "__main__":
    try:
        main()
    except psycopg.Error as exc:
        # Never echo connection strings or arbitrary server detail into shared terminals.
        raise SystemExit(f"Database operation failed: {type(exc).__name__}, SQLSTATE={exc.sqlstate}") from None
