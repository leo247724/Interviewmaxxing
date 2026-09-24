"""Find-and-save protocol tests: private temporary DBs and a fake loopback service."""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from interviewmaxxing_core import JobListing, ListingSource, listing_id_for, utc_now
from interviewmaxxing_jobs import JobStore

SCRIPT = Path(__file__).resolve().parents[2] / "skills/find-jobs/scripts/find_jobs.py"
SPEC = importlib.util.spec_from_file_location("find_jobs_skill", SCRIPT)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def listing(number="1", **changes):
    source = changes.pop("source", "linkedin")
    url = changes.pop("posting_url", f"https://www.linkedin.com/jobs/view/{number}/")
    now = utc_now()
    provenance = ListingSource(source=source, source_listing_id=number, posting_url=url,
                               source_url=url, observed_at=now, evidence="Observed current employer job page",
                               employer_job_key=changes.pop("employer_job_key", None),
                               application_url=changes.get("application_url"))
    return JobListing(id=listing_id_for(source, number, url), source=source,
                      source_listing_id=number, posting_url=url, source_url=url,
                      title=changes.pop("title", f"Performance Marketing Manager {number}"),
                      company=changes.pop("company", f"Example {number}"),
                      location=changes.pop("location", "Austin, TX"),
                      work_arrangement=changes.pop("work_arrangement", "HYBRID"),
                      status=changes.pop("status", "OPEN"),
                      description=changes.pop("description", "Owns paid media, acquisition and CAC."),
                      description_completeness=changes.pop("description_completeness", "FULL"),
                      observed_at=now, evidence="Observed on employer job page", provenance=[provenance],
                      **changes)


def reviewed(job, **changes):
    return {"listing": job.model_dump(mode="json"), "decision": changes.pop("decision", "SAVE"),
            "compensation_basis": changes.pop("compensation_basis", "employer" if job.compensation else "unknown"),
            "fit_rationale": "Owns acquisition-channel execution and paid media budgets.",
            "evidence": {name: f"Observed {name} evidence at {job.posting_url}" for name in helper.EVIDENCE_KEYS},
            "review_reasons": [], **changes}


def card(identity="baseline", *, lane="saved", listing_id=None):
    return {"id": identity, "lane": lane, "listingId": listing_id, "revision": 1,
            "fields": {"company": "Prior company", "role": identity, "processSourceNotes": "Keep exactly"},
            "createdAt": helper.utc(), "applicationUrl": None}


@pytest.fixture
def environment(tmp_path):
    db = tmp_path / "jobs.sqlite3"
    store = JobStore(db)
    store.close()
    state = SimpleNamespace(entries=[card()], requests=[], fail_update=False, wrong_db=False,
                            view_override={}, db=db, root=tmp_path / "run", scope_path=tmp_path / "scope.json")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def job(self, identity):
            rows, aliases = helper.db_snapshot(db)
            job = rows.get(helper.resolve(identity, aliases))
            if not job or state.wrong_db:
                return None
            linked = next((c["id"] for c in state.entries if c["listingId"] == job.id), None)
            pay = job.compensation

            def timestamp(value):
                return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

            return {"id": job.id, "company": job.company, "title": job.title,
                    "location": job.location, "workArrangement": job.work_arrangement.value,
                    "description": job.description, "descriptionCompleteness": job.description_completeness.value,
                    "status": job.status.value, "remoteEligibility": job.remote_eligibility,
                    "postingUrl": job.posting_url, "pipelineEntryId": linked,
                    "observedAt": timestamp(job.observed_at), "postedText": job.posted_text,
                    "compensation": {"rawText": pay.raw_text, "minimum": pay.minimum, "maximum": pay.maximum,
                                     "currency": pay.currency, "period": pay.period.value if pay.period else None}
                    if pay else None,
                    "provenance": [{"source": p.source, "sourceUrl": p.source_url, "postingUrl": p.posting_url,
                                    "applicationUrl": p.application_url, "observedAt": timestamp(p.observed_at)}
                                   for p in job.provenance], **state.view_override}

        def do_GET(self):
            state.requests.append(("GET", self.path))
            if self.path == "/pipeline":
                self.respond(200, {"entries": state.entries, "lanes": [{"id": "saved"}]})
            elif self.path.startswith("/jobs/"):
                value = self.job(self.path.split("/")[2])
                self.respond(200 if value else 404, value or {"error": "missing"})
            else:
                self.respond(404, {})

        def do_POST(self):
            state.requests.append(("POST", self.path))
            assert self.headers["Origin"] == state.origin
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.endswith("/track"):
                identity = self.path.split("/")[2]
                value = self.job(identity)
                existing = next((c for c in state.entries if c["listingId"] == identity), None)
                if existing:
                    self.respond(200, value)
                    return
                new = card(f"pipe_{len(state.entries)}", listing_id=identity)
                new["fields"] = {"company": value["company"], "role": value["title"]}
                state.entries.append(new)
                self.respond(201, self.job(identity))
            elif self.path.startswith("/pipeline/entries/"):
                if state.fail_update:
                    state.fail_update = False
                    self.respond(500, {"error": "temporary test failure"})
                    return
                entry = next(c for c in state.entries if c["id"] == self.path.split("/")[-1])
                if entry["revision"] != data["revision"]:
                    self.respond(409, {"error": "revision"})
                    return
                entry["fields"] = data["fields"]
                entry["revision"] += 1
                self.respond(200, entry)
            else:
                self.respond(404, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.api_url = f"http://127.0.0.1:{server.server_port}"
    state.origin = "http://127.0.0.1:4317"
    state.scope = {"locations": ["Austin"], "arrangements": ["HYBRID", "ONSITE"],
                   "role_focus": "Performance marketing acquisition ownership.",
                   "minimum_compensation": {"amount": 100000, "currency": "USD", "period": "YEAR"},
                   "pay_policy": "review_uncertain", "availability_policy": "review_unknown"}

    def initialize(target=2, **scope_changes):
        state.scope.update(scope_changes)
        state.scope_path.write_text(json.dumps(state.scope))
        result = invoke(state, "init", "--scope", state.scope_path, "--target", target,
                        "--jobs-db", db, "--api-url", state.api_url, "--origin", state.origin)
        state.run_id = result["run_id"]
        return result

    state.initialize = initialize
    yield state
    server.shutdown()
    server.server_close()
    thread.join()


def invoke(env, command, *args):
    parsed = helper.parser().parse_args([command, "--run-dir", str(env.root), *map(str, args)])
    with helper.locked(env.root) as root:
        return getattr(helper, command)(parsed, root)


def artifact(env, rows, name="reviewed.json"):
    path = env.root / name
    helper.private_write(path, {"schema_version": helper.REVIEW_SCHEMA, "run_id": env.run_id,
                                "reviewer": "test-reviewer", "records": rows})
    return path


def preview(env, *rows, name="reviewed.json"):
    return invoke(env, "preview", artifact(env, rows, name))["plan"]


def saves(env):
    return [path for method, path in env.requests if method == "POST"]


def test_stdlib_help_and_actionable_native_dependency_error():
    help_result = subprocess.run([sys.executable, "-S", str(SCRIPT), "--help"], capture_output=True, text=True)
    assert help_result.returncode == 0
    schema_result = subprocess.run([sys.executable, "-S", str(SCRIPT), "schema"], capture_output=True, text=True)
    assert schema_result.returncode == 2
    assert "active worktree" in schema_result.stderr


def test_init_idempotent_settings_baseline_and_private_permissions(environment):
    env = environment
    first = env.initialize()
    before = (env.root / "baseline.json").read_bytes()
    env.entries.append(card("unrelated", lane="closed"))
    second = env.initialize()
    assert first["started_at"] == second["started_at"]
    assert before == (env.root / "baseline.json").read_bytes()
    assert env.root.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in env.root.iterdir() if path.is_file())
    with pytest.raises(helper.RunError, match="different immutable settings"):
        env.initialize(target=3)


@pytest.mark.parametrize("change", [
    {"work_arrangement": "REMOTE"}, {"location": "Boston, MA"}, {"status": "CLOSED"},
    {"compensation": {"minimum": 50000, "maximum": 99000, "currency": "USD", "period": "YEAR"}},
])
def test_invalid_save_aborts_all_preflight_without_native_or_api_writes(environment, change):
    env = environment
    env.initialize()
    with pytest.raises(helper.RunError):
        preview(env, reviewed(listing("1")), reviewed(listing("2", **change)))
    assert not saves(env)
    assert not helper.db_snapshot(env.db)[0]
    assert helper.read_json(env.root / "run.json")["status"] == "running"


def test_provisional_and_missing_evidence_are_rejected(environment):
    env = environment
    env.initialize()
    path = env.root / "raw.json"
    path.write_text(json.dumps({"records": [{"listing": listing().model_dump(mode="json"), "eligible": True}]}))
    with pytest.raises(helper.RunError, match="explicit"):
        invoke(env, "preview", path)
    row = reviewed(listing())
    row["evidence"]["role"] = ""
    with pytest.raises(helper.RunError, match="six evidence"):
        preview(env, row)
    assert not saves(env)


def test_pay_review_and_unknown_availability_flags(environment):
    env = environment
    env.initialize()
    job = listing("1", status="UNKNOWN", compensation={"minimum": 90000, "maximum": 120000,
                                                          "currency": "USD", "period": "YEAR"})
    result = invoke(env, "save", "--plan", preview(env, reviewed(job), reviewed(listing("2"))))
    assert result["saved_count"] == 2
    assert result["compensation_review_count"] == 2
    assert result["availability_review_count"] == 1
    assert any("Compensation review" in c["fields"].get("processSourceNotes", "") for c in env.entries)


def test_strict_pay_requires_employer_minimum_and_rejects_estimates(environment):
    env = environment
    env.initialize(pay_policy="confirmed_floor")
    pay = {"minimum": 90000, "maximum": 120000, "currency": "USD", "period": "YEAR"}
    with pytest.raises(helper.RunError, match="confirmed_floor"):
        preview(env, reviewed(listing(compensation=pay)))
    pay["minimum"] = 100000
    with pytest.raises(helper.RunError, match="raw_text only"):
        preview(env, reviewed(listing(compensation=pay), compensation_basis="estimated"))
    plan = preview(env, reviewed(listing(compensation=pay)))
    assert invoke(env, "save", "--plan", plan)["compensation_review_count"] == 0


def test_confirmed_open_rejects_unknown(environment):
    env = environment
    env.initialize(availability_policy="confirmed_open")
    with pytest.raises(helper.RunError, match="confirmed_open"):
        preview(env, reviewed(listing(status="UNKNOWN")))


def test_cross_file_dedupes_identity_title_and_ats_but_not_generic_application(environment):
    env = environment
    env.initialize(target=5)
    first = listing("1", employer_job_key="ats:lever:example:abc", application_url="https://example.com/apply")
    same_ats = listing("2", source="google", employer_job_key="ats:lever:example:abc")
    same_title = listing("3", title=first.title, company=first.company)
    separate = listing("4", application_url="https://example.com/apply")
    a = artifact(env, [reviewed(first)], "a.json")
    b = artifact(env, [reviewed(first), reviewed(same_ats), reviewed(same_title), reviewed(separate)], "b.json")
    receipt = invoke(env, "preview", a, b)
    assert receipt["actions"] == {"SAVE": 2, "HOLD": 3}
    assert invoke(env, "save", "--plan", receipt["plan"])["saved_count"] == 2


def test_alias_and_all_lanes_existing_cards_never_refreshed(environment):
    env = environment
    old = listing("old", source="google", employer_job_key="ats:lever:example:abc")
    direct = listing("new", employer_job_key="ats:lever:example:abc")
    store = JobStore(env.db)
    store.upsert(old)
    canonical = store.upsert(direct)
    store.close()
    env.entries.append(card("prior_closed", lane="closed", listing_id=old.id))
    env.initialize()
    original = copy.deepcopy(env.entries)
    plan = preview(env, reviewed(direct))
    result = invoke(env, "save", "--plan", plan)
    assert result["saved_count"] == 0 and env.entries == original
    assert not saves(env)
    invoke(env, "index")
    indexed = helper.read_json(env.root / "index.json")["entries"]
    match = next(e for e in indexed if e["canonical_id"] == canonical.id)
    assert old.id in match["aliases"] and match["cards"][0]["lane"] == "closed"


def test_target_cap_unrelated_cards_not_counted_and_idempotent_resave(environment):
    env = environment
    env.initialize(target=1)
    original = copy.deepcopy(env.entries[0])
    env.entries.append(card("concurrent", listing_id="other_listing"))
    plan = preview(env, reviewed(listing("1")), reviewed(listing("2")))
    first = invoke(env, "save", "--plan", plan)
    posts = list(saves(env))
    second = invoke(env, "save", "--plan", plan)
    assert first["saved_count"] == second["saved_count"] == 1
    assert second["ui_saved_total"] == 3
    assert saves(env) == posts and env.entries[0] == original
    assert len(helper.db_snapshot(env.db)[0]) == 1


def test_partial_update_failure_resumes_own_card_without_retracking(environment):
    env = environment
    env.initialize(target=1)
    env.fail_update = True
    plan = preview(env, reviewed(listing()))
    with pytest.raises(helper.RunError, match="request failed"):
        invoke(env, "save", "--plan", plan)
    assert len([p for p in saves(env) if p.endswith("/track")]) == 1
    assert helper.read_json(env.root / "run.json")["status"] == "running"
    result = invoke(env, "save", "--plan", plan)
    assert result["saved_count"] == 1
    assert len([p for p in saves(env) if p.endswith("/track")]) == 1


def test_lost_track_response_holds_ambiguous_card_without_edits(environment, monkeypatch):
    env = environment
    env.initialize(target=1)
    plan = preview(env, reviewed(listing()))
    original_request = helper.Api.request
    fail = [True]

    def interrupted(api, path, data=None):
        result = original_request(api, path, data)
        if path.endswith("/track") and fail[0]:
            fail[0] = False
            raise helper.RunError("simulated lost track response")
        return result

    monkeypatch.setattr(helper.Api, "request", interrupted)
    with pytest.raises(helper.RunError, match="lost track"):
        invoke(env, "save", "--plan", plan)
    prior = copy.deepcopy(env.entries)
    with pytest.raises(helper.RunError, match="ambiguous ownership"):
        invoke(env, "save", "--plan", plan)
    assert env.entries == prior
    assert invoke(env, "report")["saved_count"] == 0
    assert len([p for p in saves(env) if p.endswith("/track")]) == 1


def test_unacknowledged_track_never_claims_or_edits_external_same_listing_card(environment, monkeypatch):
    env = environment
    env.initialize(target=1)
    job = listing()
    plan = preview(env, reviewed(job))
    original_request = helper.Api.request

    def interrupted_before_send(api, path, data=None):
        if path.endswith("/track"):
            raise helper.RunError("simulated interruption before sending")
        return original_request(api, path, data)

    monkeypatch.setattr(helper.Api, "request", interrupted_before_send)
    with pytest.raises(helper.RunError, match="before sending"):
        invoke(env, "save", "--plan", plan)
    monkeypatch.setattr(helper.Api, "request", original_request)
    external = card("concurrent_user_card", listing_id=job.id)
    env.entries.append(external)
    prior = copy.deepcopy(env.entries)
    with pytest.raises(helper.RunError, match="ambiguous ownership"):
        invoke(env, "save", "--plan", plan)
    assert env.entries == prior and not saves(env)
    assert invoke(env, "report")["saved_count"] == 0


def test_pending_intent_reserves_target_capacity(environment):
    env = environment
    env.initialize(target=1)
    first = preview(env, reviewed(listing("1")), name="first.json")
    env.fail_update = True
    with pytest.raises(helper.RunError):
        invoke(env, "save", "--plan", first)
    second = preview(env, reviewed(listing("2")), name="second.json")
    result = invoke(env, "save", "--plan", second)
    assert result["saved_count"] == 0 and result["pending_intents"] == 1
    assert len([p for p in saves(env) if p.endswith("/track")]) == 1
    assert invoke(env, "save", "--plan", first)["saved_count"] == 1


def test_closed_untracked_pending_intent_releases_capacity_without_ledger_edits(environment):
    env = environment
    env.initialize(target=1)
    job = listing("1")
    first = preview(env, reviewed(job), name="first.json")
    env.wrong_db = True
    with pytest.raises(helper.RunError, match="request failed"):
        invoke(env, "save", "--plan", first)
    env.wrong_db = False
    store = JobStore(env.db)
    store.upsert(listing("1", status="CLOSED"))
    store.close()
    result = invoke(env, "save", "--plan", first)
    assert result["saved_count"] == 0 and result["pending_intents"] == 0
    assert not saves(env)
    second = preview(env, reviewed(listing("2")), name="second.json")
    assert invoke(env, "save", "--plan", second)["saved_count"] == 1
    assert helper.read_json(env.root / "ledger.json")["intents"][job.id]["phase"] == "held"


def test_closed_unacknowledged_tracking_intent_releases_capacity(environment, monkeypatch):
    env = environment
    env.initialize(target=1)
    first = preview(env, reviewed(listing("1")), name="first.json")
    original_request = helper.Api.request

    def interrupted_before_send(api, path, data=None):
        if path.endswith("/track"):
            raise helper.RunError("simulated interruption before sending")
        return original_request(api, path, data)

    monkeypatch.setattr(helper.Api, "request", interrupted_before_send)
    with pytest.raises(helper.RunError, match="before sending"):
        invoke(env, "save", "--plan", first)
    monkeypatch.setattr(helper.Api, "request", original_request)
    store = JobStore(env.db)
    store.upsert(listing("1", status="CLOSED"))
    store.close()
    result = invoke(env, "save", "--plan", first)
    assert result["saved_count"] == result["pending_intents"] == 0
    second = preview(env, reviewed(listing("2")), name="second.json")
    assert invoke(env, "save", "--plan", second)["saved_count"] == 1


@pytest.mark.parametrize("decision", ["HOLD", "EXCLUDE"])
def test_reviewer_withdraws_open_untracked_pending_approval(environment, decision):
    env = environment
    env.initialize(target=1)
    job = listing("1")
    first = preview(env, reviewed(job), name="first.json")
    env.wrong_db = True
    with pytest.raises(helper.RunError, match="request failed"):
        invoke(env, "save", "--plan", first)
    env.wrong_db = False
    withdrawn = reviewed(job, decision=decision)
    withdrawn["fit_rationale"] = "Re-review shows campaign ownership belongs to another team."
    replacement = preview(env, reviewed(listing("2")), withdrawn, name="replacement.json")
    result = invoke(env, "save", "--plan", replacement)
    assert result["saved_count"] == 1 and result["pending_intents"] == 0
    assert helper.read_json(env.root / "ledger.json")["intents"][job.id]["phase"] == "held"
    assert len([p for p in saves(env) if p.endswith("/track")]) == 1


def test_reviewer_withdrawal_never_moves_an_acknowledged_tracked_card(environment):
    env = environment
    env.initialize(target=1)
    job = listing("1")
    invoke(env, "save", "--plan", preview(env, reviewed(job), name="first.json"))
    before = copy.deepcopy(env.entries)
    posts = list(saves(env))
    hold = preview(env, reviewed(job, decision="HOLD"), name="withdrawn.json")
    with pytest.raises(helper.RunError, match="already tracked card"):
        invoke(env, "save", "--plan", hold)
    assert env.entries == before and saves(env) == posts


def test_tampered_input_or_plan_requires_new_preflight(environment):
    env = environment
    env.initialize()
    plan = preview(env, reviewed(listing()))
    path = env.root / "reviewed.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(helper.RunError, match="input changed"):
        invoke(env, "save", "--plan", plan)
    plan = invoke(env, "preview", path)["plan"]
    Path(plan).write_text(Path(plan).read_text() + " ")
    with pytest.raises(helper.RunError, match="Plan is"):
        invoke(env, "save", "--plan", plan)
    assert not saves(env)


def test_existing_untracked_rich_description_retained(environment):
    env = environment
    rich = listing("1", description="Full detailed employer responsibilities and qualifications.")
    store = JobStore(env.db)
    store.upsert(rich)
    store.close()
    env.initialize(target=1)
    short = listing("1", description="Short teaser", description_completeness="PARTIAL")
    result = invoke(env, "save", "--plan", preview(env, reviewed(short)))
    assert result["saved_count"] == 1
    assert helper.db_snapshot(env.db)[0][rich.id].description == rich.description


def test_changed_baseline_and_wrong_service_fail_before_tracking(environment):
    env = environment
    env.initialize()
    plan = preview(env, reviewed(listing()))
    env.entries[0]["fields"]["role"] = "User edited existing card"
    with pytest.raises(helper.RunError, match="original pipeline"):
        invoke(env, "save", "--plan", plan)
    assert not helper.db_snapshot(env.db)[0]
    env.entries[0]["fields"]["role"] = "baseline"
    env.wrong_db = True
    with pytest.raises(helper.RunError, match="request failed"):
        invoke(env, "save", "--plan", plan)
    assert not saves(env)


@pytest.mark.parametrize("drift", [
    {"compensation": {"rawText": "Employer pay", "minimum": 70000, "maximum": 90000,
                      "currency": "USD", "period": "YEAR"}},
    {"descriptionCompleteness": "PARTIAL"}, {"remoteEligibility": "Canada"},
    {"provenance": []},
])
def test_same_id_divergent_service_evidence_cannot_be_tracked(environment, drift):
    env = environment
    env.initialize(target=1)
    pay = {"minimum": 110000, "maximum": 140000, "currency": "USD", "period": "YEAR"}
    plan = preview(env, reviewed(listing(compensation=pay)))
    env.view_override = drift
    with pytest.raises(helper.RunError, match="same canonical listing"):
        invoke(env, "save", "--plan", plan)
    assert not saves(env)
    assert invoke(env, "report")["saved_count"] == 0


def test_holds_exclusions_are_durable_without_closed_clutter(environment):
    env = environment
    env.initialize()
    plan = preview(env, reviewed(listing("1", status="CLOSED"), decision="EXCLUDE"),
                   reviewed(listing("2"), decision="HOLD"),
                   reviewed(listing("3"), duplicate_of="baseline"))
    result = invoke(env, "save", "--plan", plan)
    assert result["saved_count"] == 0 and result["non_saved_decisions"] == 3
    assert len(env.entries) == 1 and not saves(env)


def test_finish_requires_target_fresh_matching_total_ui_and_no_application_routes(environment):
    env = environment
    env.initialize(target=1)
    snapshot = env.root / "snapshot.txt"
    snapshot.write_text('heading "Saved 1"')
    args = ("--ui-snapshot", snapshot, "--ui-observed-at", helper.utc(),
            "--ui-url", env.origin + "/pipeline")
    with pytest.raises(helper.RunError, match="exactly the target"):
        invoke(env, "finish", *args)
    invoke(env, "save", "--plan", preview(env, reviewed(listing())))
    with pytest.raises(helper.RunError, match="total Saved heading"):
        invoke(env, "finish", "--ui-snapshot", snapshot, "--ui-observed-at", helper.utc(),
               "--ui-url", env.origin + "/pipeline")
    assert helper.read_json(env.root / "run.json")["status"] == "running"
    snapshot.write_text('heading "Saved 2" [level=2]')
    with pytest.raises(helper.RunError, match="fresh"):
        invoke(env, "finish", "--ui-snapshot", snapshot, "--ui-observed-at", "2000-01-01T00:00:00Z",
               "--ui-url", env.origin + "/pipeline")
    result = invoke(env, "finish", "--ui-snapshot", snapshot, "--ui-observed-at", helper.utc(),
                    "--ui-url", env.origin + "/pipeline")
    assert result["status"] == "complete" and result["saved_count"] == 1
    assert result["ui_saved_total"] == 2 and len(result["saved"]) == 1
    assert all(path.endswith("/track") or path.startswith("/pipeline/entries/") for path in saves(env))
    assert helper.read_json(env.root / "final-receipt.json")["elapsed_seconds"] > 0


def test_api_rejects_application_routes_and_remote_origins(environment):
    with pytest.raises(helper.RunError, match="loopback"):
        helper.Api({"api_url": "https://example.com", "origin": "http://127.0.0.1:4317"})
    api = helper.Api({"api_url": environment.api_url, "origin": environment.origin})
    with pytest.raises(helper.RunError, match="boundary"):
        api.request("/applications", {})


def test_ledger_writes_are_compact_durable_private_and_round_trip(environment, monkeypatch):
    env = environment
    env.initialize(target=1)
    plan = preview(env, reviewed(listing("1")), reviewed(listing("2"), decision="HOLD"))
    invoke(env, "save", "--plan", plan)
    raw = (env.root / "ledger.json").read_text()
    ledger = helper.read_json(env.root / "ledger.json")
    assert raw.endswith("\n") and "\n" not in raw[:-1]
    assert raw == json.dumps(ledger, separators=(",", ":"), ensure_ascii=False) + "\n"
    assert ledger["intents"] and ledger["decisions"] and ledger["plans"]
    assert (env.root / "ledger.json").stat().st_mode & 0o777 == 0o600
    # Other run state keeps its readable layout.
    assert (env.root / "run.json").read_text().startswith("{\n  ")

    synced = []
    real_fsync = helper.os.fsync
    monkeypatch.setattr(helper.os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    legacy = {"intents": {"x": {"phase": "saved", "note": "é"}}, "decisions": {}, "plans": {}}
    (env.root / "ledger.json").write_text(json.dumps(legacy, indent=2))
    helper.write_ledger(env.root, helper.read_json(env.root / "ledger.json"))
    assert helper.read_json(env.root / "ledger.json") == legacy
    assert len(synced) == 2  # file contents, then the directory entry after the rename
    assert not [p for p in env.root.iterdir() if p.name.startswith(".write-")]


def test_non_saved_decisions_are_compact_references_recoverable_from_the_pinned_plan(environment):
    env = environment
    env.initialize(target=1)
    plan = preview(env, reviewed(listing("1")), reviewed(listing("2"), decision="HOLD"),
                   reviewed(listing("3", status="CLOSED"), decision="EXCLUDE"),
                   reviewed(listing("4"), duplicate_of="baseline"))
    result = invoke(env, "save", "--plan", plan)
    ledger = helper.read_json(env.root / "ledger.json")
    assert result["saved_count"] == 1 and result["non_saved_decisions"] == len(ledger["decisions"]) == 3
    for ref in ledger["decisions"].values():
        assert "record" not in ref and "retained_listing" not in ref
        assert ref["action"] in {"HOLD", "EXCLUDE"} and ref["input_listing_id"]
        assert ref["plan"] in ledger["plans"]
        assert helper.file_digest(ref["plan"]) == ledger["plans"][ref["plan"]]
        full = next(a for a in helper.read_json(ref["plan"])["actions"]
                    if (a["input_path"], a["input_row"]) == (ref["input_path"], ref["input_row"]))
        assert helper.digest(full["record"]) == full["record_hash"] == ref["record_hash"]
        assert full["input_listing_id"] == ref["input_listing_id"]
    duplicate = next(r for r in ledger["decisions"].values() if r.get("duplicate_of"))
    assert duplicate["duplicate_of"] == "baseline" and duplicate["reason"] == "explicit duplicate"
    # Re-saving the same plan is idempotent: same keys, no growth.
    invoke(env, "save", "--plan", plan)
    assert helper.read_json(env.root / "ledger.json")["decisions"] == ledger["decisions"]


def test_verified_saved_intent_drops_pre_track_card_list(environment):
    env = environment
    env.initialize(target=1)
    plan = preview(env, reviewed(listing("1")))
    invoke(env, "save", "--plan", plan)
    intent = helper.read_json(env.root / "ledger.json")["intents"][listing("1").id]
    assert intent["phase"] == "saved" and intent["pipeline_id"]
    assert "before_track_card_ids" not in intent
