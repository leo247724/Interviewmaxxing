"""WP9 round 5: the saved listing's location reaches every batch job.

``prepare-batch`` looks each row's listing up in the jobs store and passes its location
(``apply --job-location``); the runner writes it on the job right after the request is
recorded (``ApplicationStore.record_listing``); ``bind_job_identity`` keeps it over the
page's locality; ``--retry`` carries it; the metro rule then reads it. Everything here is
fictional: the candidate, the employers, the jobs store and the pages."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from interviewmaxxing_browser.ai import AIFormRouter, BoundedDecisions, DynamicPacketResolver
from interviewmaxxing_browser.semantics import classify as classify_semantics
from interviewmaxxing_candidate.simple_answers import SimpleAnswers
from interviewmaxxing_cli import batch as batch_module
from interviewmaxxing_cli.batch import (
    BatchRow,
    LedgerEntry,
    append_ledger,
    default_jobs_db,
    listing_argv,
    read_ledger,
    with_saved_listings,
)
from interviewmaxxing_cli.main import EXIT_OK, build_parser, main
from interviewmaxxing_cli.retry import plan_retry
from interviewmaxxing_cli.runner import (
    ListingDetails,
    LocalApplicationRunner,
    NoninteractiveInteraction,
    RunLimits,
)
from interviewmaxxing_core import (
    ApplicationField,
    ApplicationForm,
    ApplicationPacket,
    ApplicationState,
    ApplicationStore,
    BrowserOptions,
    CandidateProfile,
    ChoiceValue,
    ControlType,
    FieldFillResult,
    FieldFillStatus,
    FieldOption,
    FillResult,
    IdentityEvidenceKind,
    JobIdentityObservation,
    LocalPaths,
    PageInspection,
    PageKind,
    PostalAddress,
    SavedAnswer,
)
from interviewmaxxing_core.store import LOCATION_EVENT
from interviewmaxxing_jobs.sources.base import make_listing
from interviewmaxxing_jobs.store import JobStore
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

S = ApplicationState
ORIGIN = "https://jobs.fictional.example/brambleway"
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
METRO = "Round Rock, Cedar Park, Leander, Pflugerville, Georgetown, Hutto"
MODE_Q = "Which work arrangement do you prefer?"
MODES = ("Remote", "Hybrid", "On-site")
HYBRID = "Round Rock, TX (Hybrid)"
REMOTE = "Remote (US)"


@pytest.fixture
def paths(isolated_imx_home: LocalPaths) -> LocalPaths:
    isolated_imx_home.ensure()
    return isolated_imx_home


def identity(job: str, location: str | None) -> JobIdentityObservation:
    return JobIdentityObservation(ats_type="mock", ats_tenant="brambleway", external_job_id=job,
                                  evidence_kind=IdentityEvidenceKind.ATS_JOB_ID_ON_PAGE,
                                  evidence=f"Job {job} on the fictional page",
                                  title="Page Title", location=location)


def location_events(store: ApplicationStore, app_id: str) -> list[dict[str, Any]]:
    return [e.metadata for e in store.list_events(app_id) if e.event == LOCATION_EVENT]


def bind(store: ApplicationStore, app_id: str, seen: JobIdentityObservation) -> None:
    claim = store.claim(app_id, "test")
    if store.get_application(app_id).state is S.REQUESTED:
        store.transition(claim, S.INSPECTING)
    store.bind_job_identity(claim, seen)
    store.release(claim)


# --- the store: whose location wins --------------------------------------------------------


def test_a_listing_location_fills_the_job_and_a_page_locality_never_replaces_it(paths):
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("default", f"{ORIGIN}/one").application
        job = store.record_listing(app.id, location=f"  {REMOTE} ", title="Listing Title",
                                   company="Brambleway", actor="test")
        assert (job.location, job.title, job.company) == (REMOTE, "Listing Title", "Brambleway")
        bind(store, app.id, identity("one", "Austin, TX"))  # the company's office in JSON-LD
        job = store.get_job(store.get_application(app.id).job_id)
        assert job.location == REMOTE and job.title == "Page Title"  # the page's title wins
        assert location_events(store, app.id) == [
            {"job_id": job.id, "location": REMOTE, "source": "listing", "previous": None}]


def test_a_page_locality_fills_a_job_without_one_and_a_listing_replaces_it(paths):
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("default", f"{ORIGIN}/two").application
        bind(store, app.id, identity("two", "Austin, TX"))
        job_id = store.get_application(app.id).job_id
        assert store.get_job(job_id).location == "Austin, TX"
        assert store.record_listing(app.id, location=HYBRID).location == HYBRID
        # The first listing location is kept; a later page never replaces it.
        assert store.record_listing(app.id, location="Somewhere Else, CA").location == HYBRID
        bind(store, app.id, identity("two", "Denver, CO"))
        assert store.get_job(job_id).location == HYBRID
        assert [(e["source"], e["location"], e["previous"])
                for e in location_events(store, app.id)] == [
            ("page", "Austin, TX", None), ("listing", HYBRID, "Austin, TX")]


def test_a_location_recorded_before_sources_counts_as_a_page(paths):
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("default", f"{ORIGIN}/legacy").application
    with sqlite3.connect(paths.state_db) as db:  # bound by an older version: no event
        db.execute("UPDATE jobs SET location = 'Austin, TX'")
    with ApplicationStore.open(paths.state_db) as store:
        assert store.record_listing(app.id, location=REMOTE).location == REMOTE


def test_a_merged_jobs_listing_location_replaces_the_canonical_jobs_page_locality(paths):
    with ApplicationStore.open(paths.state_db) as store:
        first = store.record_request("c1", f"{ORIGIN}/posting").application
        bind(store, first.id, identity("seven", "Austin, TX"))  # the canonical job: a page's
        alias = store.record_request("c2", f"{ORIGIN}/alias-of-posting").application
        store.record_listing(alias.id, location=REMOTE)
        bind(store, alias.id, identity("seven", "Austin, TX"))  # the same job: merged
        canonical = store.get_application(first.id).job_id
        assert store.get_application(alias.id).job_id == canonical
        assert store.get_job(canonical).location == REMOTE
        assert location_events(store, alias.id)[-1] == {
            "job_id": canonical, "location": REMOTE, "source": "listing",
            "previous": "Austin, TX"}


def test_record_listing_ignores_blanks_and_needs_no_claim(paths):
    with ApplicationStore.open(paths.state_db) as store:
        app = store.record_request("default", f"{ORIGIN}/blank").application
        claim = store.claim(app.id, "another run")
        job = store.record_listing(app.id, location="  ", title="", company=None)
        assert (job.location, job.title, job.company) == (None, None, None)
        assert location_events(store, app.id) == []
        assert store.record_listing(app.id, location=HYBRID).location == HYBRID  # while claimed
        store.release(claim)


# --- the runner: the listing's location reaches the metro rule ---------------------------------


class QuietJev:
    """Jev for these runs: every field keeps its heuristic reading (a question for the
    person, explicit answer) and every other decision takes its no-answer choice."""

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"type": "noul", "noul": 0.0}
                continue
            criteria = list(question["criteria"])
            if name[0] in "rnusd" and name[1:].isdigit():
                control = request["state"]["fields"][f"f{name[1:]}"]["control"]
                choice = {"r": "HUMAN_INPUT", "n": "literal", "u": "EXPLICIT_ANSWER",
                          "d": "APPLICATION_ATTACHMENT",
                          "s": {"TEXTAREA": "CUSTOM_LONG_TEXT", "TEXT": "CUSTOM_TEXT"}.get(
                              control, "CUSTOM_BOOLEAN")}[name[0]]
            else:
                choice = next(c for c in ("NONE", "UNKNOWN", "hold", criteria[0]) if c in criteria)
            answers[name] = {"type": "choice", "choice": choice, "confidence": 1.0,
                             "probabilities": {k: float(k == choice) for k in criteria}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


class OnePage:
    """A final-step form the runner fills and stops at (preparation only)."""

    def __init__(self, page: PageInspection) -> None:
        self.page = page

    async def open(self, url: str) -> PageInspection:
        return self.page

    async def inspect(self) -> PageInspection:
        return self.page

    async def fill(self, form: ApplicationForm, packet: ApplicationPacket) -> FillResult:
        return FillResult(form_step=form.step, fields=[
            FieldFillResult(field_id=a.field_id, status=FieldFillStatus.FILLED)
            for a in packet.answers])

    async def close(self) -> None:
        return None


class OnePageFactory:
    def __init__(self, page: PageInspection) -> None:
        self.page = page

    async def start(self, options: BrowserOptions) -> OnePage:
        return OnePage(self.page)


class Candidates:
    def __init__(self, profile: CandidateProfile) -> None:
        self.profile = profile

    def load(self, candidate_id: str) -> CandidateProfile:
        return self.profile.model_copy(update={"id": candidate_id})

    def save_answer(self, candidate_id: str, answer: SavedAnswer) -> None:
        raise AssertionError("nothing is saved in these runs")


def austin(base: CandidateProfile) -> CandidateProfile:
    """The fictional candidate in Austin, TX with the metro towns and a remote preference."""
    identity_ = base.identity.model_copy(update={"address": PostalAddress(
        city="Austin", region="TX", postal_code="78701", country="United States")})
    data = SimpleAnswers.from_identity(identity_).model_dump()
    data.update(metro_area=METRO, work_arrangement_preference="remote")
    imported = SimpleAnswers.model_validate(data).saved_answer_updates(confirmed_at=NOW)
    return base.model_copy(update={"identity": identity_,
                                   "saved_answers": [*base.saved_answers, *imported]})


def mode_page(url: str, page_location: str | None) -> PageInspection:
    field = ApplicationField(
        id="mode", selector="#mode", label=MODE_Q, control_type=ControlType.SELECT,
        semantic_type=classify_semantics(label=MODE_Q, control_type=ControlType.SELECT),
        required=True, options=[FieldOption(value=f"v{i}", label=m) for i, m in enumerate(MODES)])
    form = ApplicationForm(url=url, step=0, fields=[field], is_final_step=True,
                           submit_selector="#submit")
    return PageInspection(kind=PageKind.APPLICATION_FORM, observed_url=url, form=form,
                          job_identity=identity(url.rsplit("/", 1)[-1], page_location))


def metro_runner(paths: LocalPaths, candidate: CandidateProfile,
                 page: PageInspection) -> LocalApplicationRunner:
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-round5-key", source="test"),
                                           transport=QuietJev(), max_attempts=1))
    return LocalApplicationRunner(
        paths=paths, interaction=NoninteractiveInteraction(), headless=True,
        browser_factory=OnePageFactory(page), candidates=Candidates(candidate),
        resolver=DynamicPacketResolver(decisions, router=AIFormRouter(decisions)),
        limits=RunLimits(max_steps=4, max_same_form=2), prepare_only=True)


def chosen_mode(paths: LocalPaths, app_id: str) -> str:
    with ApplicationStore.open(paths.state_db) as store:
        packet = store.latest_packet(app_id)
    assert packet is not None
    answer = packet.answer_for("mode")
    assert answer is not None and isinstance(answer.value, ChoiceValue)
    return answer.value.label


@pytest.mark.parametrize(("listing", "page_location", "mode", "job_location"), [
    (HYBRID, None, "Hybrid", HYBRID),  # an Austin-metro hybrid posting whose page names no place
    (REMOTE, "Austin, TX", "Remote", REMOTE),  # the page's office never makes it an Austin job
    (None, "Austin, TX", "Hybrid", "Austin, TX"),  # without a listing the page's locality counts
    (None, None, "Remote", None),  # no location anywhere: remote, as before this round
])
def test_the_listing_location_reaches_the_packet_and_the_metro_rule(
    paths, fictional_candidate, listing, page_location, mode, job_location,
):
    url = f"{ORIGIN}/{abs(hash((listing, page_location)))}"
    runner = metro_runner(paths, austin(fictional_candidate), mode_page(url, page_location))
    result = asyncio.run(runner.apply(url, candidate_id="c1",
                                      listing=ListingDetails(location=listing)))
    assert result.state is S.NEEDS_INPUT and result.message.startswith("Prepared")
    assert chosen_mode(paths, result.application_id) == mode
    with ApplicationStore.open(paths.state_db) as store:
        job = store.get_job(store.get_application(result.application_id).job_id)
        assert job.location == job_location
        sources = [e["source"] for e in location_events(store, result.application_id)]
    assert sources == (["listing"] if listing else ["page"] if page_location else [])


def test_resume_writes_the_listing_location_before_the_run(paths, fictional_candidate):
    url = f"{ORIGIN}/resumed"
    runner = metro_runner(paths, austin(fictional_candidate), mode_page(url, None))
    first = asyncio.run(runner.apply(url, candidate_id="c1"))
    assert chosen_mode(paths, first.application_id) == "Remote"  # no location: remote
    again = asyncio.run(runner.resume(first.application_id,
                                      listing=ListingDetails(location=HYBRID)))
    assert again.state is S.NEEDS_INPUT and chosen_mode(paths, first.application_id) == "Hybrid"


def test_apply_and_resume_accept_the_listing_flags():
    parser = build_parser()
    args = parser.parse_args(["apply", f"{ORIGIN}/x", "--job-location", HYBRID,
                              "--job-title", "Growth Lead", "--job-company", "Brambleway"])
    assert (args.job_location, args.job_title, args.job_company) == (HYBRID, "Growth Lead",
                                                                      "Brambleway")
    resumed = parser.parse_args(["resume", "app_x", "--job-location", REMOTE])
    assert (resumed.job_location, resumed.job_title) == (REMOTE, None)
    assert not ListingDetails(location=" ", title=None) and ListingDetails(location=REMOTE)


# --- prepare-batch: the jobs store, the argv, the ledger and --retry ----------------------------


def saved_listing(jobs: JobStore, number: int, location: str | None, *,
                  title: str = "Growth Marketing Manager", company: str | None = "Brambleway") -> str:
    """A fictional saved listing; returns its id (the inventory's ``listing_id``)."""
    sid = f"48000000{number:02d}"
    posting = f"https://jobs.linkedin.test/view/{sid}"
    return jobs.upsert(make_listing(
        source="linkedin", source_listing_id=sid, posting_url=posting, source_url=posting,
        title=title, company=company, location=location, observed_at=NOW, query_id="qry_round5",
        evidence="fictional round 5 observation")).id


def test_rows_take_their_saved_listings_location_from_the_jobs_store(paths):
    jobs_db = default_jobs_db(paths)
    assert jobs_db == paths.home / "jobs" / "jobs.sqlite3"
    jobs = JobStore(jobs_db)
    hybrid = saved_listing(jobs, 1, HYBRID)
    remote = saved_listing(jobs, 2, REMOTE, company="Quillfield")
    placeless = saved_listing(jobs, 3, None)
    jobs.close()
    before = jobs_db.read_bytes()
    rows = [BatchRow(listing_id=hybrid, application_url=f"{ORIGIN}/a", company="Brambleway",
                     title="Growth Marketing Manager"),
            BatchRow(listing_id=remote, application_url=f"{ORIGIN}/b"),  # no title/company
            BatchRow(listing_id=placeless, application_url=f"{ORIGIN}/c"),
            BatchRow(listing_id="lst_not_saved", application_url=f"{ORIGIN}/d"),
            BatchRow(listing_id=f"url:{ORIGIN}/e", application_url=f"{ORIGIN}/e"),
            BatchRow(listing_id=hybrid, application_url=f"{ORIGIN}/f", location="Kept, TX")]
    updated, found = with_saved_listings(rows, jobs_db)
    assert found == 2
    assert [(r.location, r.title, r.company) for r in updated] == [
        (HYBRID, "Growth Marketing Manager", "Brambleway"),
        (REMOTE, "Growth Marketing Manager", "Quillfield"),  # filled where the row had none
        ("", "Growth Marketing Manager", "Brambleway"),  # a listing without a location
        ("", "", ""), ("", "", ""),  # no listing; keyed by URL
        ("Kept, TX", "", "")]  # a row's own location (a retry's ledger line) is kept
    assert jobs_db.read_bytes() == before  # only read
    assert listing_argv(updated[0]) == ["--job-listing-id", hybrid, "--job-location", HYBRID,
                                        "--job-title", "Growth Marketing Manager",
                                        "--job-company", "Brambleway"]
    assert listing_argv(updated[3]) == ["--job-listing-id", "lst_not_saved"]  # apply finds none
    assert listing_argv(updated[4]) == []  # keyed by its URL: no saved listing

    missing = paths.home / "elsewhere" / "jobs.sqlite3"
    assert with_saved_listings(rows, missing) == (rows, 0)
    assert not missing.exists() and not missing.parent.exists()  # never created


def test_the_jobs_store_can_be_named_by_imx_jobs_db(paths, tmp_path, monkeypatch):
    monkeypatch.setenv("IMX_JOBS_DB", str(tmp_path / "custom" / "jobs.sqlite3"))
    assert default_jobs_db(paths) == tmp_path / "custom" / "jobs.sqlite3"


LOCATION_FAKE_CLI = textwrap.dedent('''\
    """Fake ``interviewmaxxing apply URL`` / ``resume APP`` over the real store: records the
    request and the listing flags the way the runner does, then stops held on one question.
    Logs its argv to FAKE_LOG."""
    import json, os, sys

    from interviewmaxxing_core import ApplicationState as S, ApplicationStore, LocalPaths

    argv = sys.argv[1:]
    with open(os.environ["FAKE_LOG"], "a") as fh:
        fh.write(json.dumps(argv) + "\\n")

    def flag(name):
        return argv[argv.index(name) + 1] if name in argv else None

    with ApplicationStore.open(LocalPaths.from_env().state_db) as store:
        if argv[0] == "apply":
            app_id = store.record_request(flag("--candidate"), argv[1]).application.id
        else:
            app_id = argv[1]
        store.record_listing(app_id, location=flag("--job-location"), title=flag("--job-title"),
                             company=flag("--job-company"), actor="fake")
        url = store.list_requests(app_id)[0].application_url
        claim = store.claim(app_id, "fake")
        if store.get_application(app_id).state is not S.INSPECTING:
            store.transition(claim, S.INSPECTING)
        held = {"field_id": "q", "form_url": url, "form_step": 0, "field_fingerprint": "ab" * 32,
                "label": "What is your notice period?", "reason": "NO_ANSWER",
                "prompt": "Please answer.", "required": True, "control_type": "TEXT"}
        store.transition(claim, S.NEEDS_INPUT, metadata={"missing_inputs": [held],
                                                         "reason": "missing answers"})
        store.release(claim)
    print(json.dumps({"application_id": app_id, "state": "NEEDS_INPUT", "receipt": None,
                      "missing_inputs": [held], "message": "1 required question(s) need your answer."}))
    sys.exit(3)
''')


@pytest.mark.slow
def test_prepare_batch_passes_the_listing_location_and_a_retry_carries_it(
        paths, tmp_path, monkeypatch, capsys):
    script = tmp_path / "location_fake_cli.py"
    script.write_text(LOCATION_FAKE_CLI)
    log = tmp_path / "fake.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setattr(batch_module, "default_command", lambda: [sys.executable, str(script)])
    jobs = JobStore(default_jobs_db(paths))
    hybrid, remote = saved_listing(jobs, 1, HYBRID), saved_listing(jobs, 2, REMOTE)
    jobs.close()
    inventory = tmp_path / "inventory.json"
    inventory.write_text(json.dumps([
        {"listing_id": listing, "company": "Brambleway", "title": "Growth Lead",
         "source_application_url": f"{ORIGIN}/{name}", "backend": "mock", "status": "resolved"}
        for listing, name in ((hybrid, "hybrid"), (remote, "remote"), ("lst_unsaved", "none"))]))
    home = ["--home", str(paths.home)]
    assert main([*home, "prepare-batch", "--inventory", str(inventory), "--batch-id", "b1",
                 "--json"]) == EXIT_OK
    assert "2 with their saved listing's location" in capsys.readouterr().err
    applied = {argv[1].rsplit("/", 1)[-1]: argv for argv in map(json.loads, log.read_text().splitlines())}
    assert applied["hybrid"][-6:] == ["--job-location", HYBRID, "--job-title", "Growth Lead",
                                      "--job-company", "Brambleway"]
    assert applied["remote"][applied["remote"].index("--job-location") + 1] == REMOTE
    assert [applied[n][applied[n].index("--job-listing-id") + 1] for n in ("hybrid", "remote")] == \
        [hybrid, remote]
    assert "--job-location" not in applied["none"]
    ledger = {e.listing_id: e for e in read_ledger(paths.home / "batches" / "b1" / "ledger.jsonl")}
    assert (ledger[hybrid].location, ledger[remote].location, ledger["lst_unsaved"].location) == \
        (HYBRID, REMOTE, "")
    with ApplicationStore.open(paths.state_db) as store:
        located = {e.listing_id: store.get_job(store.get_application(
            e.application_id or "").job_id).location for e in ledger.values()}
    assert located == {hybrid: HYBRID, remote: REMOTE, "lst_unsaved": None}

    # A retry passes the ledger's location again (resume --job-location).
    log.write_text("")
    assert main([*home, "prepare-batch", "--retry", "b1", "--batch-id", "r1", "--all",
                 "--json"]) == EXIT_OK
    assert "2 with their listing's location" in capsys.readouterr().err
    resumed = [json.loads(line) for line in log.read_text().splitlines()]
    assert {argv[0] for argv in resumed} == {"resume"}
    assert sorted(argv[argv.index("--job-location") + 1] for argv in resumed
                  if "--job-location" in argv) == sorted([HYBRID, REMOTE])
    retried = read_ledger(paths.home / "batches" / "r1" / "ledger.jsonl")
    assert {e.listing_id: e.location for e in retried} == {
        hybrid: HYBRID, remote: REMOTE, "lst_unsaved": ""}


def test_a_retry_of_an_older_ledger_takes_the_location_from_the_jobs_store(paths):
    jobs = JobStore(default_jobs_db(paths))
    hybrid = saved_listing(jobs, 1, HYBRID)
    jobs.close()
    with ApplicationStore.open(paths.state_db) as store:  # a failed run, recorded before round 5
        app = store.record_request("default", f"{ORIGIN}/older").application
        claim = store.claim(app.id, "test")
        store.transition(claim, S.INSPECTING)
        store.transition(claim, S.FAILED_RETRYABLE, failure_reason="Stopped (fictional).")
        store.release(claim)
    append_ledger(paths.home / "batches" / "old" / "ledger.jsonl", LedgerEntry(
        batch_id="old", listing_id=hybrid, application_url=f"{ORIGIN}/older", attempt=1,
        application_id=app.id, outcome="failed_retryable", state=S.FAILED_RETRYABLE,
        started_at=NOW - timedelta(seconds=5), finished_at=NOW, duration_s=5.0))
    [item] = plan_retry(paths, "old", candidate_id="default",
                        jobs_db=default_jobs_db(paths)).items
    assert item.row.location == HYBRID
    [unlooked] = plan_retry(paths, "old", candidate_id="default").items
    assert unlooked.row.location == ""
