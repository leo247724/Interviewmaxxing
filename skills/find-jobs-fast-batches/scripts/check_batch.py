"""Dry-run a candidates/reviewed file exactly like find_jobs.py preview's
record validation + SAVE gate, plus a duplicate check against index.json.
No writes. Prints ONE short line per record and a summary (small stdout).

usage: <imx_python> check_batch.py --run-dir RUN FILE [FILE...]
"""
import argparse
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--run-dir", required=True)
ap.add_argument("files", nargs="+")
a = ap.parse_args()
helper = Path(__file__).resolve().parents[2] / "find-jobs/scripts/find_jobs.py"
if not helper.is_file():
    ap.error("find-jobs helper missing beside this skill; install the complete Interviewmaxxing skill bundle")
spec = importlib.util.spec_from_file_location("find_jobs", helper)
fj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fj)
with open(Path(a.run_dir) / "run.json") as fh:
    run = json.load(fh)
scope = run["config"]["scope"]
with open(Path(a.run_dir) / "index.json") as fh:
    index = json.load(fh)["entries"]
key_owner = {k: e for e in index for k in e.get("keys", [])}
sig_owner = {tuple(e["signature"]): e for e in index if e.get("signature")}
JobListing = fj.native()[0]
bad = 0
seen = {}
tmpdir = tempfile.TemporaryDirectory(prefix="imx-check-")  # private 0700 dir
for f in a.files:
    with open(f) as fh:
        p = json.load(fh)
    env_ok = (set(p) == {"schema_version", "run_id", "reviewer", "records"}
              and p["schema_version"] == fj.REVIEW_SCHEMA and p["run_id"] == run["run_id"])
    if not env_ok:
        print(f"{Path(f).name}: ENVELOPE-INVALID (keys/schema_version/run_id)")
        bad += 1
        continue
    for n, row in enumerate(p["records"], 1):
        tag = f"{Path(f).name}#{n}"
        try:
            # Reuse the helper's exact per-record validation on a 1-record payload.
            tmp = Path(tmpdir.name) / f"row-{n}.json"
            tmp.write_text(json.dumps({**p, "records": [row]}))
            fj.reviewed_records([str(tmp)], run)
            L = JobListing.model_validate(row["listing"])
            keys = set(fj.keys_for(L))
            dup = [key_owner[k] for k in keys if k in key_owner]
            sig = sig_owner.get(tuple(fj.signature(L.company, L.title) or ()))
            batch_dup = [k for k in keys if k in seen]
            for k in keys:
                seen.setdefault(k, tag)
            note = ""
            if dup:
                note += f" DUP-INDEX:{dup[0]['canonical_id']}"
            elif sig:
                note += f" SAME-COMPANY+TITLE:{sig['canonical_id']}(must HOLD)"
            if batch_dup:
                note += f" DUP-IN-BATCH:{seen[batch_dup[0]]}"
            if row["decision"] == "SAVE" and (dup or sig or batch_dup):
                bad += 1
                note += " <-- SAVE on duplicate"
            print(f"{tag} {row['decision']} ok {L.company[:28] if L.company else '?'} | {L.title[:40]} | {L.work_arrangement.value} {L.location or ''}{note}")
        except Exception as e:
            bad += 1
            print(f"{tag} {row.get('decision')} INVALID: {str(e)[:220]}")
tmpdir.cleanup()
print(f"SUMMARY files={len(a.files)} problems={bad}")
sys.exit(1 if bad else 0)
