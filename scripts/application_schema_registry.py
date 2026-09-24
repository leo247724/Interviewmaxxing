#!/usr/bin/env python3
"""Publish label-only backend maps to Supabase and export its local runtime cache.

Use the existing private psycopg connection JSON; credentials never enter argv or
output. Writes are bounded to application_schema_maps and explicitly named files.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
from pathlib import Path
from typing import Any

BACKEND = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")


def export_url_index(inventory: Path, output: Path) -> dict[str, int]:
    """Resolve exact Saved URLs without guessing when classifications conflict."""
    # The separate database-operator venv has psycopg but not application runtime
    # dependencies. Reuse the pure-stdlib canonical normalizer from this checkout.
    source = Path(__file__).resolve().parents[1] / "packages/core/src/interviewmaxxing_core/urls.py"
    spec = importlib.util.spec_from_file_location("imx_registry_urls", source)
    if spec is None or spec.loader is None:
        raise ValueError("Canonical URL normalizer is unavailable")
    normalizer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(normalizer)

    body = json.loads(inventory.read_text())
    index: dict[str, str] = {}
    conflicts: set[str] = set()
    repeated = 0
    for backend, rows in body["by_backend"].items():
        if not BACKEND.fullmatch(backend):
            raise ValueError("Invalid inventory backend")
        for row in rows:
            url = normalizer.normalize_application_url(row["url"])
            if url in index:
                repeated += 1
                if index[url] != backend:
                    conflicts.add(url)
            index[url] = backend
    for url in conflicts:
        del index[url]
    destination = output / "_url_index.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(index, sort_keys=True, indent=2) + "\n")
    temporary.replace(destination)
    return {"indexed_urls": len(index), "repeated_urls": repeated,
            "conflicting_urls_omitted": len(conflicts)}


def read_map(path: Path) -> dict[str, Any]:
    body = path.read_bytes()
    if len(body) > 512_000:
        raise ValueError(f"Map is too large: {path.name}")
    data = json.loads(body)
    if (not isinstance(data, dict) or not BACKEND.fullmatch(str(data.get("backend", "")))
            or path.stem != data["backend"] or not data.get("schema_version")):
        raise ValueError(f"Invalid map identity: {path.name}")
    samples = data.get("observed_samples", [])
    if not isinstance(samples, list) or any(not isinstance(s, dict) for s in samples):
        raise ValueError(f"Invalid observed samples: {path.name}")
    if not isinstance(data.get("observed"), dict):
        raise ValueError(f"Missing observed/unknown separation: {path.name}")
    coverage = data.get("coverage", {})
    declared = str(coverage.get("status", data.get("status", "partial")))
    # A recorded blocker is a useful map record, never evidence of complete forms.
    status = ("manual" if data["backend"] == "email" else
              "blocked" if "blocked" in declared else
              "observed" if declared in {"observed", "complete"} and samples else "partial")
    return {"backend": data["backend"], "schema_version": str(data["schema_version"]),
            "map_sha256": hashlib.sha256(json.dumps(
                data, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "coverage_status": status,
            "observed_samples": len(samples), "map": data}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["publish", "export", "status"])
    parser.add_argument("--connection-file", type=Path,
                        default=Path(".imx/application-urls/admin-connection.json"))
    parser.add_argument("--maps", type=Path, default=Path("docs/application-schemas"))
    parser.add_argument("--output", type=Path,
                        default=Path(".imx/dynamic-applications/schema-cache"))
    parser.add_argument("--inventory", type=Path,
                        help="with export, also index exact classified Saved URLs")
    args = parser.parse_args()
    # Optional operator dependency, kept off the browser/runtime import path.
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    connect = json.loads(args.connection_file.read_text())
    with psycopg.connect(**connect, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set local statement_timeout='15s'")
        if args.command == "publish":
            records = [read_map(p) for p in sorted(args.maps.glob("*.json"))
                       if BACKEND.fullmatch(p.stem)]
            for row in records:
                cur.execute("""insert into public.application_schema_maps
                    (backend,schema_version,map_sha256,coverage_status,observed_samples,map)
                    values (%(backend)s,%(schema_version)s,%(map_sha256)s,%(coverage_status)s,
                            %(observed_samples)s,%(map)s)
                    on conflict (backend) do update set schema_version=excluded.schema_version,
                    map_sha256=excluded.map_sha256,coverage_status=excluded.coverage_status,
                    observed_samples=excluded.observed_samples,map=excluded.map,updated_at=now()
                    where application_schema_maps.map_sha256 is distinct from excluded.map_sha256
                """, {**row, "map": Jsonb(row["map"])})
        cur.execute("select backend,coverage_status,observed_samples,map_sha256,map "
                    "from public.application_schema_maps order by backend")
        rows = cur.fetchall()
        index_summary = None
        if args.command == "export":
            args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
            for row in rows:
                destination = args.output / f"{row['backend']}.json"
                temporary = destination.with_suffix(".json.tmp")
                temporary.write_text(json.dumps(row["map"], indent=2) + "\n")
                temporary.replace(destination)
            if args.inventory:
                index_summary = export_url_index(args.inventory, args.output)
        print(json.dumps({"command": args.command, "backend_records": len(rows),
            "url_index": index_summary,
            "records": [{k: row[k] for k in ("backend", "coverage_status", "observed_samples")}
                        for row in rows]}))


if __name__ == "__main__":
    main()
