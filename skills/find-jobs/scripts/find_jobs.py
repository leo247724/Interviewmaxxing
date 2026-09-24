#!/usr/bin/env python3
"""Private, resumable find-and-save accounting. No browser or application actions.

Run with the active Interviewmaxxing worktree's Python environment. ``--help``
needs only the standard library; ``schema`` exposes the installed native contract.
Evidence truth and semantic fit are reviewer responsibilities, not script claims.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

REVIEW_SCHEMA = "imx.find-jobs.reviewed.v1"
RUN_SCHEMA = "imx.find-jobs.run.v1"
EVIDENCE_KEYS = ("role", "location", "arrangement", "availability", "compensation", "identity")
RECORD_KEYS = {"listing", "decision", "fit_rationale", "evidence", "review_reasons",
               "duplicate_of", "compensation_basis"}
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


class RunError(ValueError):
    """An actionable failed precondition; never completes or resets a run."""


def native():
    try:
        from interviewmaxxing_core import JobListing, normalize_application_url
        from interviewmaxxing_jobs import JobStore
        from interviewmaxxing_jobs.text import employer_key_from_url
    except ImportError as exc:
        raise RunError(
            "Native Interviewmaxxing packages are unavailable. Run from the active worktree "
            "with its .venv/bin/python, or uv run --all-packages python; install the "
            "workspace dependencies there before retrying. --help needs no packages."
        ) from exc
    return JobListing, JobStore, normalize_application_url, employer_key_from_url


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def utc(epoch=None):
    return dt.datetime.fromtimestamp(time.time() if epoch is None else epoch,
                                     dt.UTC).isoformat().replace("+00:00", "Z")


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise RunError(f"Cannot read JSON file: {path}: {exc}") from exc


def private_write(path, value, *, compact=False):
    """Atomic owner-only JSON, including a durable rename before side effects.

    ``compact`` drops indentation for large, frequently rewritten state (the ledger);
    durability and permissions are identical either way."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            if compact:
                json.dump(value, stream, separators=(",", ":"), ensure_ascii=False)
            else:
                json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_ledger(root, ledger):
    private_write(Path(root) / "ledger.json", ledger, compact=True)


def decision_ref(action, plan):
    """Compact non-SAVE ledger decision. The full reviewed record stays in the
    sha-pinned plan (``ledger["plans"]``), whose action for ``input_path``/``input_row``
    carries the same ``record_hash``; the ledger keeps only the decision itself."""
    ref = {key: action[key] for key in ("action", "input_listing_id", "record_hash", "reviewer",
                                        "input_path", "input_row") if key in action}
    for key in ("reason", "duplicate_of"):
        if action.get(key) is not None:
            ref[key] = action[key]
    ref["plan"] = str(plan)
    return ref


def private_copy(source, destination):
    data = Path(source).read_bytes()
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


@contextlib.contextmanager
def locked(run_dir):
    root = Path(run_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunError("Another coordinator owns this run. Retry after its command finishes.") from exc
        yield root
    finally:
        os.close(fd)


def loopback_url(value, label):
    parts = urllib.parse.urlsplit(value)
    if (parts.scheme not in {"http", "https"}
            or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parts.username or parts.password or parts.query or parts.fragment
            or parts.path not in {"", "/"}):
        raise RunError(f"{label} must be a loopback http(s) origin without credentials or a path")
    try:
        _ = parts.port
    except ValueError as exc:
        raise RunError(f"Invalid {label} port") from exc
    return value.rstrip("/")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RunError("Local service redirected a request; check the configured API URL")


class Api:
    def __init__(self, config):
        self.base = loopback_url(config["api_url"], "API URL")
        self.origin = loopback_url(config["origin"], "Origin")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, data=None):
        allowed = (path == "/pipeline" or re.fullmatch(r"/jobs/[A-Za-z0-9_-]+", path))
        if data is not None:
            allowed = bool(re.fullmatch(r"/jobs/[A-Za-z0-9_-]+/track", path)
                           or re.fullmatch(r"/pipeline/entries/[A-Za-z0-9_-]+", path))
        if not allowed:
            raise RunError(f"Endpoint is outside the find-and-save boundary: {path}")
        headers = {"Origin": self.origin}
        payload = None
        if data is not None:
            payload = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data=payload, headers=headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.load(response), response.status
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise RunError(f"Local service request failed: {request.get_method()} {path}: {exc}") from exc

    def board(self):
        value, _ = self.request("/pipeline")
        if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
            raise RunError("Pipeline response lacks an entries list")
        ids = [row.get("id") for row in value["entries"]]
        if None in ids or len(ids) != len(set(ids)):
            raise RunError("Pipeline returned missing or duplicate entry IDs")
        return value


def scope_config(value):
    required = {"locations", "arrangements", "role_focus", "minimum_compensation",
                "pay_policy", "availability_policy"}
    if not isinstance(value, dict) or set(value) != required:
        raise RunError("Scope must contain exactly: " + ", ".join(sorted(required)))
    result = dict(value)
    for key in ("locations", "arrangements"):
        if (not isinstance(result[key], list) or not result[key]
                or any(not isinstance(x, str) or not x.strip() for x in result[key])):
            raise RunError(f"Scope {key} must be nonempty strings")
        result[key] = list(dict.fromkeys(x.strip() for x in result[key]))
    result["arrangements"] = [x.upper() for x in result["arrangements"]]
    if not set(result["arrangements"]) <= {"ONSITE", "HYBRID", "REMOTE"}:
        raise RunError("Scope arrangements must be ONSITE, HYBRID or REMOTE")
    if not isinstance(result["role_focus"], str) or not result["role_focus"].strip():
        raise RunError("Scope role_focus must explain the responsibility focus")
    if result["pay_policy"] not in {"review_uncertain", "confirmed_floor"}:
        raise RunError("pay_policy must be review_uncertain or confirmed_floor")
    if result["availability_policy"] not in {"review_unknown", "confirmed_open"}:
        raise RunError("availability_policy must be review_unknown or confirmed_open")
    floor = result["minimum_compensation"]
    if (not isinstance(floor, dict) or set(floor) != {"amount", "currency", "period"}
            or isinstance(floor["amount"], bool) or not isinstance(floor["amount"], (int, float))
            or not math.isfinite(floor["amount"]) or floor["amount"] <= 0
            or not isinstance(floor["currency"], str)
            or not re.fullmatch(r"[A-Za-z]{3}", floor["currency"])
            or floor["period"] not in {"YEAR", "MONTH", "WEEK", "DAY", "HOUR"}):
        raise RunError("minimum_compensation needs a positive amount, currency and explicit period")
    result["minimum_compensation"] = {**floor, "currency": floor["currency"].upper()}
    return result


def load_run(root):
    run = read_json(root / "run.json")
    if run.get("schema_version") != RUN_SCHEMA:
        raise RunError("Run has an unsupported schema; initialize a new private run directory")
    if file_digest(root / "baseline.json") != run["baseline_sha256"]:
        raise RunError("Immutable baseline hash changed; restore the original baseline before continuing")
    return run, read_json(root / "ledger.json")


def unchanged(root, run, board):
    original = read_json(root / "baseline.json")["entries"]
    current = {row["id"]: row for row in board["entries"]}
    if any(current.get(row["id"]) != row for row in original):
        raise RunError("An original pipeline entry changed or disappeared; no further saves or completion")


def db_snapshot(path):
    JobListing, _, _, _ = native()
    path = Path(path)
    if not path.is_file():
        raise RunError("Jobs DB does not exist. Verify the active service's canonical jobs DB path")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
            listings = {row[0]: JobListing.model_validate_json(row[1]) for row in connection.execute(
                "SELECT id, data FROM listings")}
            aliases = dict(connection.execute("SELECT alias_id, listing_id FROM listing_aliases"))
    except sqlite3.Error as exc:
        raise RunError(f"Cannot read the configured native jobs DB: {exc}") from exc
    return listings, aliases


def resolve(identity, aliases):
    seen = set()
    while identity in aliases:
        if identity in seen:
            raise RunError("Native alias cycle; inspect the jobs store before continuing")
        seen.add(identity)
        identity = aliases[identity]
    return identity


def norm(value):
    return re.sub(r"[^\w]+", "", (value or "").casefold())


def signature(company, title):
    return [norm(company), norm(title)] if company and title else None


def keys_for(listing):
    _, _, normalize_url, employer_key = native()
    keys = set(listing.identity_keys)
    for source in listing.provenance:
        if source.posting_url:
            with contextlib.suppress(ValueError):
                keys.add("url:" + normalize_url(source.posting_url))
        for url in (source.posting_url, source.application_url):
            key = employer_key(url)
            if key:
                keys.add("job:" + key)
    return sorted(keys)


def build_index(board, listings, aliases):
    _, _, _, employer_key = native()
    cards = {}
    for card in board["entries"]:
        cid = resolve(card.get("listingId"), aliases)
        cards.setdefault(cid, []).append(card)
    result = []
    for cid, listing in listings.items():
        result.append({"canonical_id": cid, "aliases": sorted(k for k in aliases
                       if resolve(k, aliases) == cid), "company": listing.company,
                       "title": listing.title, "signature": signature(listing.company, listing.title),
                       "keys": keys_for(listing), "posting_url": listing.posting_url,
                       "cards": [{"id": c["id"], "lane": c["lane"]} for c in cards.get(cid, [])]})
    for card in board["entries"]:
        cid = resolve(card.get("listingId"), aliases)
        if cid in listings:
            continue
        fields = card.get("fields", {})
        key = employer_key(card.get("applicationUrl"))
        result.append({"canonical_id": cid, "aliases": [], "company": fields.get("company"),
                       "title": fields.get("role"),
                       "signature": signature(fields.get("company"), fields.get("role")),
                       "keys": ["job:" + key] if key else [], "posting_url": None,
                       "cards": [{"id": card["id"], "lane": card["lane"]}]})
    return result


def reviewed_records(files, run):
    JobListing, _, _, _ = native()
    records, inputs = [], []
    for filename in files:
        path = Path(filename).expanduser().resolve()
        payload = read_json(path)
        if (not isinstance(payload, dict)
                or set(payload) != {"schema_version", "run_id", "reviewer", "records"}
                or payload.get("schema_version") != REVIEW_SCHEMA
                or payload.get("run_id") != run["run_id"]
                or not isinstance(payload.get("reviewer"), str) or not payload["reviewer"].strip()
                or not isinstance(payload.get("records"), list)):
            raise RunError(f"{path.name}: expected an explicit {REVIEW_SCHEMA} artifact for this run")
        inputs.append({"path": str(path), "sha256": file_digest(path)})
        for number, row in enumerate(payload["records"], 1):
            label = f"{path.name} record {number}"
            if not isinstance(row, dict) or not (RECORD_KEYS - {"duplicate_of"}) <= set(row) <= RECORD_KEYS:
                raise RunError(f"{label}: missing or unrecognized reviewed fields")
            if row["decision"] not in {"SAVE", "HOLD", "EXCLUDE"}:
                raise RunError(f"{label}: decision must be SAVE, HOLD or EXCLUDE")
            if row["compensation_basis"] not in {"employer", "estimated", "unknown"}:
                raise RunError(f"{label}: compensation_basis must be employer, estimated or unknown")
            if not isinstance(row["fit_rationale"], str) or not row["fit_rationale"].strip():
                raise RunError(f"{label}: fit_rationale must be nonempty")
            evidence = row["evidence"]
            if (not isinstance(evidence, dict) or set(evidence) != set(EVIDENCE_KEYS)
                    or any(not isinstance(v, str) or not v.strip() for v in evidence.values())):
                raise RunError(f"{label}: all six evidence dimensions must be nonempty strings")
            reasons = row["review_reasons"]
            if not isinstance(reasons, list) or any(not isinstance(v, str) or not v.strip() for v in reasons):
                raise RunError(f"{label}: review_reasons must be a list of nonempty strings")
            if row.get("duplicate_of") is not None and (
                    not isinstance(row["duplicate_of"], str) or not row["duplicate_of"].strip()):
                raise RunError(f"{label}: duplicate_of must identify a listing or card")
            try:
                listing = JobListing.model_validate(row["listing"])
            except ValueError as exc:
                raise RunError(f"{label}: invalid native JobListing: {exc}") from exc
            pay = listing.compensation
            if row["compensation_basis"] != "employer" and pay and (
                    pay.minimum is not None or pay.maximum is not None):
                raise RunError(f"{label}: estimated/unknown compensation must retain raw_text only, no numeric bounds")
            if not SAFE_ID.fullmatch(listing.id):
                raise RunError(f"{label}: listing ID is not safe for a local API route")
            normalized = {**row, "listing": listing.model_dump(mode="json")}
            if row["decision"] == "SAVE" and not row.get("duplicate_of"):
                gate(listing, normalized, run["config"]["scope"])
            records.append({"record": normalized, "record_hash": digest(normalized),
                            "reviewer": payload["reviewer"], "input_path": str(path),
                            "input_row": number})
    return records, inputs


def gate(listing, record, scope):
    """Structural policy only. A nonempty rationale does not prove relevance."""
    if listing.status.value == "CLOSED":
        raise RunError(f"{listing.id}: CLOSED listings cannot be saved")
    if listing.work_arrangement.value not in scope["arrangements"]:
        raise RunError(f"{listing.id}: work arrangement is outside the run scope")
    location = " ".join((listing.location or "").casefold().split())
    if not any(re.search(r"(?<!\w)" + re.escape(" ".join(place.casefold().split())) + r"(?!\w)",
                         location) for place in scope["locations"]):
        raise RunError(f"{listing.id}: location text does not match a configured location fragment")
    floor, pay = scope["minimum_compensation"], listing.compensation
    factor = None
    if pay and pay.currency == floor["currency"] and pay.period:
        if pay.period.value == floor["period"]:
            factor = 1
        elif (pay.period.value, floor["period"]) == ("MONTH", "YEAR"):
            factor = 12
        elif (pay.period.value, floor["period"]) == ("YEAR", "MONTH"):
            factor = 1 / 12
    employer = record["compensation_basis"] == "employer"
    comparable = employer and factor is not None and pay and (
        pay.minimum is not None or pay.maximum is not None)
    low = pay.minimum * factor if comparable and pay.minimum is not None else None
    high = pay.maximum * factor if comparable and pay.maximum is not None else None
    if high is not None and high < floor["amount"]:
        raise RunError(f"{listing.id}: published pay upper bound is below the configured floor")
    confirmed = low is not None and low >= floor["amount"]
    if scope["pay_policy"] == "confirmed_floor" and not confirmed:
        raise RunError(f"{listing.id}: confirmed_floor needs employer-published comparable minimum >= floor")
    unknown_availability = listing.status.value != "OPEN"
    if unknown_availability and scope["availability_policy"] == "confirmed_open":
        raise RunError(f"{listing.id}: confirmed_open requires an OPEN observation")
    reasons = list(record["review_reasons"])
    if not confirmed:
        reasons.append("Compensation review: confirm employer pay at or above "
                       f"{floor['amount']:g} {floor['currency']} per {floor['period']} before applying.")
    if unknown_availability:
        reasons.append("Availability review: verify the current employer vacancy before applying.")
    if listing.description_completeness.value != "FULL":
        reasons.append("Requirements review: the observed description is partial or absent.")
    if not any(p.employer_job_key for p in listing.provenance):
        reasons.append("Identity review: no proven employer ATS key was supplied; check aliases/reposts before applying.")
    return {"compensation_review": not confirmed, "availability_review": unknown_availability,
            "review_reasons": list(dict.fromkeys(reasons))}


def init(args, root):
    config = {"scope": scope_config(read_json(args.scope)), "target": args.target,
              "jobs_db": str(Path(args.jobs_db).expanduser().resolve()),
              "api_url": loopback_url(args.api_url, "API URL"),
              "origin": loopback_url(args.origin, "Origin")}
    if args.target < 1:
        raise RunError("Target must be positive")
    if (root / "run.json").exists():
        run, _ = load_run(root)
        if run["config"] != config:
            raise RunError("Run already exists with different immutable settings; choose another run directory")
        return {"run_id": run["run_id"], "status": run["status"], "started_at": run["started_at"],
                "resumed": True, "run_dir": str(root)}
    started = time.time()
    db_snapshot(config["jobs_db"])
    baseline = Api(config).board()
    private_write(root / "baseline.json", baseline)
    run = {"schema_version": RUN_SCHEMA, "run_id": "find_" + uuid.uuid4().hex,
           "config": config, "started_at": utc(started), "started_epoch": started,
           "status": "running", "baseline_sha256": file_digest(root / "baseline.json"),
           "last_mutation_epoch": started}
    write_ledger(root, {"intents": {}, "decisions": {}, "plans": {}})
    private_write(root / "run.json", run)
    return {"run_id": run["run_id"], "started_at": run["started_at"], "status": "running",
            "run_dir": str(root), "baseline_entries": len(baseline["entries"])}


def current(root, run):
    api = Api(run["config"])
    board = api.board()
    unchanged(root, run, board)
    listings, aliases = db_snapshot(run["config"]["jobs_db"])
    return api, board, listings, aliases


def index(args, root):
    run, _ = load_run(root)
    _, board, listings, aliases = current(root, run)
    rows = build_index(board, listings, aliases)
    private_write(root / "index.json", {"run_id": run["run_id"], "observed_at": utc(), "entries": rows})
    return {"index": str(root / "index.json"), "entries": len(rows),
            "note": "All lanes included. Signatures are conservative holds, not proof to merge."}


def classify(records, run, ledger, board, listings, aliases):
    JobListing, _, _, _ = native()
    existing = build_index(board, listings, aliases)
    actions = []
    for item in records:
        record = item["record"]
        listing = JobListing.model_validate(record["listing"])
        action = {**item, "input_listing_id": listing.id, "action": record["decision"]}
        if record["decision"] != "SAVE" or record.get("duplicate_of"):
            if record.get("duplicate_of"):
                action.update(action="HOLD", reason="explicit duplicate", duplicate_of=record["duplicate_of"])
            actions.append(action)
            continue
        intent = ledger["intents"].get(listing.id)
        if intent:
            if intent["record_hash"] != item["record_hash"]:
                raise RunError(f"{listing.id}: a pending/completed save already owns a different reviewed record; do not overwrite it")
            action.update(action="RESUME", canonical_id=intent.get("canonical_id"))
            actions.append(action)
            continue
        keys = set(keys_for(listing))
        sig = signature(listing.company, listing.title)
        matches = [entry for entry in existing if (
            entry["canonical_id"] == resolve(listing.id, aliases)
            or listing.id in entry["aliases"] or keys.intersection(entry["keys"]))]
        protected = next((m for m in matches if m["cards"] or m.get("planned")), None)
        same_title = next((m for m in existing if sig and sig == m["signature"] and m not in matches), None)
        if protected or same_title or len(matches) > 1:
            match = protected or same_title or matches[0]
            action.update(action="HOLD", reason="existing or ambiguous duplicate",
                          duplicate_of=match["canonical_id"] or match["cards"][0]["id"])
        else:
            retained = listing
            if matches:
                stored = listings[matches[0]["canonical_id"]]
                if not stored.is_same_posting(listing):
                    action.update(action="HOLD", reason="identity conflict; review canonical evidence",
                                  duplicate_of=stored.id)
                    actions.append(action)
                    continue
                retained = stored.merged_with(listing)
                if record["compensation_basis"] != "employer" and retained.compensation and (
                        retained.compensation.minimum is not None or retained.compensation.maximum is not None):
                    raise RunError(f"{listing.id}: retained canonical numeric pay conflicts with estimated/unknown review; inspect it before saving")
            gate(retained, record, run["config"]["scope"])
            action["retained_listing"] = retained.model_dump(mode="json")
            existing.append({"canonical_id": listing.id, "aliases": [], "keys": sorted(keys),
                             "signature": sig, "cards": [], "planned": True})
        actions.append(action)
    return actions


def preview(args, root):
    run, ledger = load_run(root)
    if run["status"] != "running":
        raise RunError("Run is complete; initialize a new run for new saves")
    records, inputs = reviewed_records(args.files, run)
    _, board, listings, aliases = current(root, run)
    actions = classify(records, run, ledger, board, listings, aliases)
    plan = {"schema_version": "imx.find-jobs.plan.v1", "run_id": run["run_id"],
            "created_at": utc(), "inputs": inputs, "actions": actions,
            "config_sha256": digest(run["config"])}
    plan_id = digest(plan)
    path = root / "plans" / f"{plan_id}.json"
    private_write(path, plan)
    ledger["plans"][str(path)] = file_digest(path)
    for action in actions:
        if action["action"] in {"HOLD", "EXCLUDE"}:
            ledger["decisions"][digest(action)] = decision_ref(action, path)
    write_ledger(root, ledger)
    saved = len(owned_entries(board, ledger, aliases))
    reserved = sum(i["phase"] not in {"saved", "held"} for i in ledger["intents"].values())
    capacity = max(0, run["config"]["target"] - saved - reserved)
    proposed = sum(action["action"] == "SAVE" for action in actions)
    return {"plan": str(path), "actions": dict(Counter(a["action"] for a in actions)),
            "target": run["config"]["target"], "current_saved": saved,
            "remaining_target": max(0, run["config"]["target"] - saved),
            "reserved_pending": reserved, "capacity_for_new_saves": capacity,
            "proposed_new_saves": min(capacity, proposed), "deferred_by_target": max(0, proposed - capacity),
            "note": "All rows validated. Semantic fit and evidence truth require reviewer judgment."}


def owned_entries(board, ledger, aliases):
    entries, seen = [], set()
    current_cards = {entry["id"]: entry for entry in board["entries"]}
    for intent in ledger["intents"].values():
        if intent["phase"] != "saved":
            continue
        card = current_cards.get(intent.get("pipeline_id"))
        cid = resolve(intent.get("canonical_id"), aliases)
        if (card and card["lane"] == "saved" and resolve(card.get("listingId"), aliases) == cid
                and cid not in seen):
            entries.append((card, intent, cid))
            seen.add(cid)
    return entries


def persist(root, run, ledger, mutation=False):
    if mutation:
        run["last_mutation_epoch"] = time.time()
    write_ledger(root, ledger)
    private_write(root / "run.json", run)


def service_listing_fields(listing):
    """Native fields exposed by the service ListingView, in its wire format."""
    def timestamp(value):
        return value.astimezone(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    pay = listing.compensation
    return {"id": listing.id, "title": listing.title, "company": listing.company,
            "location": listing.location, "workArrangement": listing.work_arrangement.value,
            "remoteEligibility": listing.remote_eligibility, "description": listing.description,
            "descriptionCompleteness": listing.description_completeness.value,
            "status": listing.status.value, "postingUrl": listing.posting_url,
            "postedText": listing.posted_text, "observedAt": timestamp(listing.observed_at),
            "compensation": {"rawText": pay.raw_text, "minimum": pay.minimum, "maximum": pay.maximum,
                             "currency": pay.currency, "period": pay.period.value if pay.period else None}
            if pay else None,
            "provenance": [{"source": source.source, "sourceUrl": source.source_url,
                            "postingUrl": source.posting_url, "applicationUrl": source.application_url,
                            "observedAt": timestamp(source.observed_at)} for source in listing.provenance]}


def verify_service_listing(api, listing):
    value, _ = api.request(f"/jobs/{listing.id}")
    expected = service_listing_fields(listing)
    if any(value.get(key) != val for key, val in expected.items()):
        raise RunError("Configured jobs DB and service do not expose the same canonical listing; no tracking performed")
    return value


def hold_pending(root, run, ledger, input_id, intent, reason):
    intent.update(phase="held", hold_reason=reason)
    ledger["decisions"][digest({"input_id": input_id, "reason": reason})] = {
        "action": "HOLD", "input_listing_id": input_id, "reason": reason}
    persist(root, run, ledger)


def save_one(root, run, ledger, action, api):
    JobListing, JobStore, _, _ = native()
    input_id = action["input_listing_id"]
    intent = ledger["intents"].get(input_id)
    if intent and intent["phase"] == "saved":
        return
    if intent is None:
        intent = {"record_hash": action["record_hash"], "record": action["record"],
                  "reviewer": action["reviewer"], "phase": "prepared", "started_at": utc(),
                  "retained_listing": action["retained_listing"]}
        ledger["intents"][input_id] = intent
        persist(root, run, ledger)
    listing = JobListing.model_validate(intent["retained_listing"])
    _, board, listings, aliases = current(root, run)
    canonical_id = resolve(intent.get("canonical_id", listing.id), aliases)
    matching_cards = [card for card in board["entries"]
                      if resolve(card.get("listingId"), aliases) == canonical_id]
    if intent["phase"] == "prepared":
        # Recheck every existing tracked identity immediately before touching the DB.
        try:
            matches = classify([{k: action[k] for k in ("record", "record_hash", "reviewer", "input_path", "input_row")}],
                               run, {**ledger, "intents": {k: v for k, v in ledger["intents"].items()
                                                           if k != input_id}}, board, listings, aliases)
        except RunError as exc:
            if not matching_cards:
                hold_pending(root, run, ledger, input_id, intent,
                             "Untracked pending intent no longer passes preflight: " + str(exc))
                return
            raise
        if matches[0]["action"] != "SAVE":
            intent["phase"] = "held"
            intent["hold_reason"] = matches[0].get("reason", "existing tracked identity")
            persist(root, run, ledger)
            return
        listing = JobListing.model_validate(matches[0]["retained_listing"])
        store = JobStore(run["config"]["jobs_db"])
        try:
            listing = store.upsert(listing, raw={"find_jobs_run": run["run_id"],
                                               "review": intent["record"], "reviewer": intent["reviewer"]},
                                   run_id=run["run_id"])
        finally:
            store.close()
        intent.update(phase="upserted", canonical_id=listing.id,
                      retained_listing=listing.model_dump(mode="json"))
        persist(root, run, ledger, mutation=True)
    elif intent["phase"] == "held":
        return
    else:
        listing = listings.get(canonical_id)
        if listing is None:
            raise RunError("A pending canonical listing disappeared; inspect the run before retrying")
    if matching_cards and intent["phase"] in {"upserted", "tracking"}:
        reason = ("Interrupted tracking has ambiguous ownership; matching cards are held without edits or counting"
                  if intent["phase"] == "tracking" else "Concurrent track targets an existing card")
        hold_pending(root, run, ledger, input_id, intent, reason)
        if "ambiguous" in reason:
            raise RunError(reason)
        return
    try:
        gate(listing, intent["record"], run["config"]["scope"])
    except RunError as exc:
        if intent["phase"] in {"upserted", "tracking"} and not matching_cards:
            hold_pending(root, run, ledger, input_id, intent,
                         "Untracked pending intent no longer passes scope policy: " + str(exc))
            return
        raise
    view = verify_service_listing(api, listing)
    board = api.board()
    unchanged(root, run, board)
    _, aliases = db_snapshot(run["config"]["jobs_db"])
    matching_cards = [card for card in board["entries"]
                      if resolve(card.get("listingId"), aliases) == listing.id]
    if intent["phase"] == "upserted":
        if matching_cards or view.get("pipelineEntryId"):
            intent.update(phase="held", hold_reason="canonical merge or concurrent track targets an existing card")
            persist(root, run, ledger)
            return
        intent.update(phase="tracking", before_track_card_ids=[c["id"] for c in board["entries"]],
                      tracking_started_at=utc())
        persist(root, run, ledger)
        tracked, status = api.request(f"/jobs/{listing.id}/track", {})
        if status != 201:
            intent.update(phase="held", hold_reason="track returned an existing card; it is not owned by this run")
            persist(root, run, ledger)
            return
        intent.update(phase="tracked", pipeline_id=tracked.get("pipelineEntryId"))
        persist(root, run, ledger, mutation=True)
    elif intent["phase"] == "tracking":
        # The API has no operation receipt/idempotency key. A card appearing after
        # a lost response could belong to another caller; time/identity is not proof.
        if not matching_cards:
            intent["phase"] = "upserted"
            persist(root, run, ledger)
            return save_one(root, run, ledger, action, api)
        else:
            reason = "Interrupted tracking has ambiguous ownership; matching cards are held without edits or counting"
            intent.update(phase="held", hold_reason=reason)
            ledger["decisions"][digest({"input_id": input_id, "reason": reason})] = {
                "action": "HOLD", "input_listing_id": input_id, "reason": reason,
                "possible_pipeline_ids": [card["id"] for card in matching_cards]}
            persist(root, run, ledger)
            raise RunError(reason)
    board = api.board()
    unchanged(root, run, board)
    cards = [card for card in board["entries"] if card["id"] == intent.get("pipeline_id")]
    if (len(cards) != 1 or cards[0]["id"] in intent.get("before_track_card_ids", [])
            or resolve(cards[0].get("listingId"), aliases) != listing.id):
        raise RunError("Tracking did not produce a verifiable new canonical pipeline entry")
    card = cards[0]
    if card["lane"] != "saved":
        raise RunError("Tracked entry is not in Saved; this helper never moves pipeline cards")
    flags = gate(listing, intent["record"], run["config"]["scope"])
    marker = f"Find-jobs run {run['run_id']}"
    fields = dict(card["fields"])
    notes = (fields.get("processSourceNotes") or "").strip()
    if marker not in notes:
        fields["processSourceNotes"] = (notes + "\n" if notes else "") + marker + ". " + " ".join(flags["review_reasons"]) + " Source: " + (listing.posting_url or listing.source_url)
    if not fields.get("fitRationale"):
        fields["fitRationale"] = intent["record"]["fit_rationale"]
    if not fields.get("nextAction"):
        fields["nextAction"] = "Review recorded uncertainties and requirements before any separately authorized application."
    if fields != card["fields"]:
        api.request(f"/pipeline/entries/{card['id']}", {"revision": card["revision"], "fields": fields})
        persist(root, run, ledger, mutation=True)
    latest = api.board()
    unchanged(root, run, latest)
    verified = next((c for c in latest["entries"] if c["id"] == card["id"]), None)
    if not verified or verified["lane"] != "saved" or verified["fields"] != fields:
        raise RunError("Saved entry/notes readback failed; retry this same plan to reconcile the intent")
    intent.update(phase="saved", saved_at=utc(), canonical_id=listing.id, **flags)
    # Only tracking verification (above) reads the pre-track card list; a verified
    # saved intent never returns to it, so it need not be rewritten on every persist.
    intent.pop("before_track_card_ids", None)
    persist(root, run, ledger, mutation=True)


def save(args, root):
    run, ledger = load_run(root)
    path = Path(args.plan).expanduser().resolve()
    if str(path) not in ledger["plans"] or file_digest(path) != ledger["plans"][str(path)]:
        raise RunError("Plan is missing, unrecognized or changed; create a fresh preview")
    plan = read_json(path)
    if plan["run_id"] != run["run_id"] or plan["config_sha256"] != digest(run["config"]):
        raise RunError("Plan belongs to different run settings")
    for item in plan["inputs"]:
        if file_digest(item["path"]) != item["sha256"]:
            raise RunError("Reviewed input changed after preview; create a fresh preview before saving")
    # Validate every row before any native DB or service mutation, including retries.
    records, _ = reviewed_records([item["path"] for item in plan["inputs"]], run)
    api, board, listings, aliases = current(root, run)
    if run["status"] == "complete":
        return summarize(root, run, ledger, board, listings, aliases)
    actions = classify(records, run, ledger, board, listings, aliases)
    planned = {(a["input_listing_id"], a["record_hash"]) for a in plan["actions"]
               if a["action"] in {"SAVE", "RESUME"}}
    # Review can withdraw an unfinished approval, including semantic judgments
    # that no structural gate can discover. Only untracked intents may retire.
    for action in actions:
        if action["record"]["decision"] not in {"HOLD", "EXCLUDE"}:
            continue
        intent = ledger["intents"].get(action["input_listing_id"])
        if not intent or intent["phase"] == "held":
            continue
        cid = resolve(intent.get("canonical_id", action["input_listing_id"]), aliases)
        matching_cards = [card for card in board["entries"]
                          if resolve(card.get("listingId"), aliases) == cid]
        if intent["phase"] in {"tracked", "saved"} or matching_cards:
            raise RunError("Review withdrawal affects an already tracked card; review it separately. "
                           "This helper never moves or retires tracked cards.")
        hold_pending(root, run, ledger, action["input_listing_id"], intent,
                     "Reviewer withdrew unfinished approval: " + action["record"]["decision"] + ". "
                     + action["record"]["fit_rationale"])
    for action in actions:
        if action["action"] not in {"SAVE", "RESUME"}:
            ledger["decisions"][digest(action)] = decision_ref(action, path)
            continue
        if (action["input_listing_id"], action["record_hash"]) not in planned:
            continue
        _, live, _, current_aliases = current(root, run)
        intent = ledger["intents"].get(action["input_listing_id"])
        needs_reconcile = intent and intent["phase"] not in {"saved", "held"}
        reserved = sum(i["phase"] not in {"saved", "held"} for i in ledger["intents"].values())
        capacity_used = len(owned_entries(live, ledger, current_aliases)) + reserved
        if capacity_used >= run["config"]["target"] and not needs_reconcile:
            continue
        save_one(root, run, ledger, action, api)
    persist(root, run, ledger)
    _, board, listings, aliases = current(root, run)
    return summarize(root, run, ledger, board, listings, aliases)


def summarize(root, run, ledger, board, listings, aliases):
    entries = owned_entries(board, ledger, aliases)
    sources, pay_review, availability_review, description_review, failures = Counter(), 0, 0, 0, []
    for _, intent, cid in entries:
        listing = listings.get(cid)
        if listing is None:
            failures.append(f"{cid}: canonical listing missing")
            continue
        sources[listing.source] += 1
        description_review += listing.description_completeness.value != "FULL"
        try:
            flags = gate(listing, intent["record"], run["config"]["scope"])
            pay_review += flags["compensation_review"]
            availability_review += flags["availability_review"]
        except RunError as exc:
            failures.append(str(exc))
    elapsed = (run.get("finished_epoch", time.time()) - run["started_epoch"])
    return {"run_id": run["run_id"], "status": run["status"], "target": run["config"]["target"],
            "saved_count": len(entries), "ui_saved_total": sum(c["lane"] == "saved" for c in board["entries"]),
            "elapsed_seconds": round(elapsed, 3), "started_at": run["started_at"],
            "compensation_review_count": pay_review, "availability_review_count": availability_review,
            "description_review_count": description_review,
            "source_counts": dict(sorted(sources.items())), "validation_errors": failures,
            "pending_intents": sum(i["phase"] not in {"saved", "held"} for i in ledger["intents"].values()),
            "non_saved_decisions": len(ledger["decisions"]), "baseline_unchanged": True,
            "run_dir": str(root)}


def report(args, root):
    run, ledger = load_run(root)
    _, board, listings, aliases = current(root, run)
    return summarize(root, run, ledger, board, listings, aliases)


def finish(args, root):
    run, ledger = load_run(root)
    _, board, listings, aliases = current(root, run)
    receipt = summarize(root, run, ledger, board, listings, aliases)
    if run["status"] == "complete":
        return receipt
    if receipt["saved_count"] != run["config"]["target"] or receipt["validation_errors"] or receipt["pending_intents"]:
        raise RunError("Finish requires exactly the target of valid run-owned Saved cards and no pending intents; timer remains running")
    try:
        observed = dt.datetime.fromisoformat(args.ui_observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("timestamp needs timezone")
        epoch = observed.timestamp()
    except (ValueError, TypeError) as exc:
        raise RunError("UI observation time must be an explicit ISO timestamp with timezone") from exc
    now = time.time()
    if epoch < run["last_mutation_epoch"] or not -10 <= now - epoch <= 300:
        raise RunError("UI snapshot must be fresh (within five minutes) and after the last save/update")
    if args.ui_url.rstrip("/") != run["config"]["origin"] + "/pipeline":
        raise RunError("UI evidence must come from this run's configured dashboard /pipeline URL")
    snapshot = Path(args.ui_snapshot).read_text()
    counts = {int(n) for n in re.findall(r"\bSaved\s+(\d+)\b", snapshot)}
    if counts != {receipt["ui_saved_total"]}:
        raise RunError("Fresh UI snapshot must show exactly the current total Saved heading, including baseline Saved cards")
    private_copy(args.ui_snapshot, root / "ui-final-snapshot.txt")
    finished = time.time()
    run.update(status="complete", finished_at=utc(finished), finished_epoch=finished,
               elapsed_seconds=round(finished - run["started_epoch"], 3))
    receipt.update(status="complete", finished_at=run["finished_at"], elapsed_seconds=run["elapsed_seconds"],
                   ui_evidence={"url": args.ui_url, "observed_at": args.ui_observed_at,
                                "sha256": file_digest(root / "ui-final-snapshot.txt")},
                   saved=[{"canonical_listing_id": cid, "pipeline_id": card["id"]}
                          for card, _, cid in owned_entries(board, ledger, aliases)])
    private_write(root / "final-receipt.json", receipt)
    private_write(root / "run.json", run)
    return {**receipt, "receipt": str(root / "final-receipt.json")}


def schema():
    JobListing, _, _, _ = native()
    listing = JobListing.model_json_schema()
    definitions = listing.pop("$defs", {})
    definitions["JobListing"] = listing
    text = {"type": "string", "minLength": 1}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
            "additionalProperties": False, "$defs": definitions,
            "required": ["schema_version", "run_id", "reviewer", "records"],
            "properties": {"schema_version": {"const": REVIEW_SCHEMA}, "run_id": text,
                           "reviewer": text, "records": {"type": "array", "items": {
                               "type": "object", "additionalProperties": False,
                               "required": sorted(RECORD_KEYS - {"duplicate_of"}),
                               "properties": {"listing": {"$ref": "#/$defs/JobListing"},
                                              "decision": {"enum": ["SAVE", "HOLD", "EXCLUDE"]},
                                              "compensation_basis": {"enum": ["employer", "estimated", "unknown"]},
                                              "fit_rationale": text, "duplicate_of": {"type": ["string", "null"]},
                                              "review_reasons": {"type": "array", "items": text},
                                              "evidence": {"type": "object", "additionalProperties": False,
                                                           "required": list(EVIDENCE_KEYS),
                                                           "properties": {key: text for key in EVIDENCE_KEYS}}}}}}}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("schema", help="Print reviewed-artifact JSON Schema including native JobListing")
    for name in ("init", "index", "preview", "save", "report", "finish"):
        command = commands.add_parser(name)
        command.add_argument("--run-dir", required=True, help="Private run directory; one coordinator writer")
        if name == "init":
            command.add_argument("--scope", required=True)
            command.add_argument("--target", required=True, type=int)
            command.add_argument("--jobs-db", required=True)
            command.add_argument("--api-url", default="http://127.0.0.1:8765")
            command.add_argument("--origin", default="http://127.0.0.1:4317")
        elif name == "preview":
            command.add_argument("files", nargs="+")
        elif name == "save":
            command.add_argument("--plan", required=True)
        elif name == "finish":
            command.add_argument("--ui-snapshot", required=True)
            command.add_argument("--ui-observed-at", required=True)
            command.add_argument("--ui-url", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "schema":
            result = schema()
        else:
            with locked(args.run_dir) as root:
                result = globals()[args.command](args, root)
        print(json.dumps(result, indent=2))
        return 0
    except (RunError, OSError, KeyError) as exc:
        print(f"find-jobs: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
