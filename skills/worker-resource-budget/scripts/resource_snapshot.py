#!/usr/bin/env python3
"""Read-only host and worker-process resource snapshot (stdlib only).

Reads one `ps` table (PID, PPID, RSS, %CPU, start time only: never command names,
arguments or environment), load average, and memory/swap (macOS vm_stat/sysctl or
Linux /proc/meminfo). It never signals, renices, kills, or changes anything.

Inventory JSON (optional): a list, or {"workers": [...]}, of
  {"worker_id": "w07", "pid": 4242, "expected_started_at": "2026-09-23T04:05:27Z"}
`expected_started_at` may be ISO-8601 (with offset/Z) or epoch seconds.

A PID whose start time matches is IDENTITY evidence only, not proof that the process
belongs to your task. Confirm task, workspace and parent independently before any
lifecycle action. Unknown metrics are reported as null, never as zero.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import math
import os
import subprocess
import sys
import tempfile
import time

PS_COLUMNS = ("pid", "ppid", "rss", "%cpu", "lstart")  # deliberately no comm/args/command/env
MB = 1024 * 1024


class SnapshotError(Exception):
    pass


def run(cmd):
    """Run a read-only command; return stdout or None when unavailable."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                                env={**os.environ, "LC_ALL": "C", "LANG": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def parse_ps(text):
    """Rows of `ps -A -o pid= -o ppid= -o rss= -o %cpu= -o lstart=` (C locale)."""
    procs = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            pid, ppid, rss_kb, cpu = int(parts[0]), int(parts[1]), int(parts[2]), float(parts[3])
        except ValueError:
            continue
        try:
            start = time.mktime(time.strptime(" ".join(parts[4:9]), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            start = None
        procs[pid] = {"pid": pid, "ppid": ppid, "rss_bytes": rss_kb * 1024, "cpu_pct": cpu, "start_epoch": start}
    return procs


def parse_expected(value):
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise SnapshotError(f"expected_started_at must be a finite epoch: {value!r}")
        return float(value)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise SnapshotError(f"expected_started_at is not ISO-8601 or epoch seconds: {value!r}") from exc
        if parsed.tzinfo is None:
            raise SnapshotError(f"expected_started_at needs a timezone offset or Z: {value!r}")
        return parsed.timestamp()
    raise SnapshotError(f"expected_started_at has unsupported type: {value!r}")


def load_inventory(path):
    try:
        with open(path) as stream:
            data = json.load(stream)
    except OSError as exc:
        raise SnapshotError(f"cannot read inventory {path}: {exc.strerror}") from exc
    except ValueError as exc:
        raise SnapshotError(f"inventory {path} is not valid JSON: {exc}") from exc
    workers = data.get("workers") if isinstance(data, dict) else data
    if not isinstance(workers, list):
        raise SnapshotError('inventory must be a JSON list or {"workers": [...]}')
    return workers


def descendants(root, children):
    seen, stack = set(), [root]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        stack.extend(children.get(pid, ()))
    return seen


def classify(workers, procs):
    """Per-worker identity status plus a deduplicated aggregate over matched process trees."""
    children = {}
    for proc in procs.values():
        children.setdefault(proc["ppid"], []).append(proc["pid"])
    claims = {}
    for entry in workers:
        pid = entry.get("pid") if isinstance(entry, dict) else None
        if isinstance(pid, int) and not isinstance(pid, bool):
            claims[pid] = claims.get(pid, 0) + 1
    rows, counted = [], set()
    for entry in workers:
        entry = entry if isinstance(entry, dict) else {}
        pid, wid = entry.get("pid"), str(entry.get("worker_id") or "?")[:64]
        row = {"worker_id": wid, "pid": pid, "status": None, "tree_pids": None,
               "tree_rss_bytes": None, "tree_cpu_pct": None}
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            row["status"] = "invalid_entry"
        elif claims[pid] > 1:
            row["status"] = "duplicate_claim"  # ambiguous: no claimant is trusted
        elif pid not in procs:
            row["status"] = "missing"
        else:
            proc = procs[pid]
            try:
                expected = parse_expected(entry.get("expected_started_at"))
            except SnapshotError as exc:
                expected, row["note"] = None, str(exc)
            if expected is None or proc["start_epoch"] is None:
                row["status"] = "unproven_identity"
            elif math.floor(expected) != int(proc["start_epoch"]):
                row["status"] = "start_mismatch"  # ps start has 1 s resolution; any other second = likely PID reuse
            else:
                row["status"] = "matched"
            tree = descendants(pid, children)
            row["tree_pids"] = len(tree)
            row["tree_rss_bytes"] = sum(procs[p]["rss_bytes"] for p in tree)
            row["tree_cpu_pct"] = round(sum(procs[p]["cpu_pct"] for p in tree), 1)
            if row["status"] == "matched":
                counted |= tree
        rows.append(row)
    aggregate = {"matched_workers": sum(r["status"] == "matched" for r in rows),
                 "unique_pids": len(counted),
                 "rss_footprint_bytes": sum(procs[p]["rss_bytes"] for p in counted) if counted else None,
                 "cpu_pct": round(sum(procs[p]["cpu_pct"] for p in counted), 1) if counted else None,
                 "note": "RSS sum is process footprint (shared pages counted per process), not unique RAM."}
    return rows, aggregate


def parse_vm_stat(text):
    if not text:
        return None
    page = None
    values = {}
    for line in text.splitlines():
        if "page size of" in line:
            digits = [w for w in line.split() if w.isdigit()]
            page = int(digits[0]) if digits else None
        elif ":" in line:
            key, _, raw = line.partition(":")
            raw = raw.strip().rstrip(".")
            if raw.isdigit():
                values[key.strip().strip('"')] = int(raw)
    if not page:
        return None
    def pick(k):
        return values[k] * page if k in values else None
    parts = [pick("Pages free"), pick("Pages inactive"), pick("Pages speculative")]
    return {"available_estimate_bytes": sum(parts) if None not in parts else None,
            "compressed_bytes": pick("Pages occupied by compressor"), "wired_bytes": pick("Pages wired down")}


def parse_swapusage(text):
    """sysctl vm.swapusage: 'total = 24576.00M  used = 19558.25M  free = ...'"""
    out = {"swap_total_bytes": None, "swap_used_bytes": None}
    words = (text or "").replace("=", " ").split()
    units = {"K": 1024, "M": MB, "G": 1024 * MB}
    for key, name in (("total", "swap_total_bytes"), ("used", "swap_used_bytes")):
        if key in words and words.index(key) + 1 < len(words):
            raw = words[words.index(key) + 1]
            with contextlib.suppress(ValueError, IndexError):
                out[name] = int(float(raw[:-1]) * units[raw[-1]]) if raw[-1] in units else int(raw)
    return out


def parse_meminfo(text):
    kb = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            kb[key] = int(fields[0]) * 1024
    total, free = kb.get("SwapTotal"), kb.get("SwapFree")
    return {"mem_total_bytes": kb.get("MemTotal"), "available_estimate_bytes": kb.get("MemAvailable"),
            "swap_total_bytes": total, "swap_used_bytes": total - free if None not in (total, free) else None}


def host_metrics(runner=run):
    host = {"cpus": os.cpu_count(), "load": None, "mem_total_bytes": None, "available_estimate_bytes": None,
            "swap_total_bytes": None, "swap_used_bytes": None, "compressed_bytes": None, "source": None}
    with contextlib.suppress(OSError):
        host["load"] = [round(x, 2) for x in os.getloadavg()]
    if sys.platform == "darwin":
        host["source"] = "sysctl+vm_stat"
        memsize = (runner(["sysctl", "-n", "hw.memsize"]) or "").strip()
        host["mem_total_bytes"] = int(memsize) if memsize.isdigit() else None
        vm = parse_vm_stat(runner(["vm_stat"])) or {}
        host["available_estimate_bytes"] = vm.get("available_estimate_bytes")
        host["compressed_bytes"] = vm.get("compressed_bytes")
        host.update(parse_swapusage(runner(["sysctl", "-n", "vm.swapusage"])))
    elif os.path.exists("/proc/meminfo"):
        host["source"] = "/proc/meminfo"
        host.update(parse_meminfo(read_text("/proc/meminfo")))
    return host


def read_text(path):
    try:
        with open(path) as stream:
            return stream.read()
    except OSError:
        return None  # metrics stay unknown


def mb(value):
    return None if value is None else round(value / MB, 1)


def summarize(host, procs, rows, aggregate, limit):
    statuses = {}
    for row in rows:
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    ranked = sorted(rows, key=lambda r: -(r["tree_rss_bytes"] or 0))
    return {"observed_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "host": {"cpus": host["cpus"], "load": host["load"], "mem_total_mb": mb(host["mem_total_bytes"]),
                     "available_estimate_mb": mb(host["available_estimate_bytes"]),
                     "compressed_mb": mb(host["compressed_bytes"]), "swap_used_mb": mb(host["swap_used_bytes"]),
                     "swap_total_mb": mb(host["swap_total_bytes"]), "source": host["source"]},
            "process_count": len(procs) if procs is not None else None,
            "inventory": statuses, "task_aggregate": {**aggregate, "rss_footprint_mb": mb(aggregate["rss_footprint_bytes"])},
            "workers": [{"worker_id": r["worker_id"], "status": r["status"], "tree_pids": r["tree_pids"],
                         "tree_rss_mb": mb(r["tree_rss_bytes"]), "tree_cpu_pct": r["tree_cpu_pct"]}
                        for r in ranked[:limit]],
            "workers_truncated": max(0, len(rows) - limit)}


def write_private(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        raise SnapshotError(f"--output directory does not exist: {directory}")
    try:
        fd, name = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
    except OSError as exc:
        raise SnapshotError(f"cannot write --output in {directory}: {exc.strerror}") from exc
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, indent=1)
            stream.write("\n")
        os.replace(name, path)
    except OSError as exc:
        raise SnapshotError(f"cannot write --output {path}: {exc.strerror}") from exc
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main(argv=None, runner=run):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--inventory", help="JSON list of {worker_id, pid, expected_started_at}")
    parser.add_argument("--output", help="write the detailed snapshot to this private (0600) JSON file")
    parser.add_argument("--limit", type=int, default=15, help="max worker rows on stdout (default 15)")
    args = parser.parse_args(argv)
    try:
        if args.limit < 0:
            raise SnapshotError("--limit must be >= 0")
        workers = load_inventory(args.inventory) if args.inventory else []
        ps_text = runner(["ps", "-A"] + [arg for col in PS_COLUMNS for arg in ("-o", col + "=")])
        procs = parse_ps(ps_text) if ps_text is not None else None
        if procs is None and workers:
            raise SnapshotError("ps is unavailable; worker identity cannot be checked")
        rows, aggregate = classify(workers, procs or {})
        host = host_metrics(runner)
        summary = summarize(host, procs, rows, aggregate, args.limit)
        if args.output:
            write_private(args.output, {"summary": summary, "host_bytes": host, "workers": rows})
            summary["detail_file"] = os.path.abspath(args.output)
    except SnapshotError as exc:
        print(f"resource_snapshot: error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
