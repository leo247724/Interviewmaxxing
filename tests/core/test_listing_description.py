"""WP9 round 6 item 4: the saved listing's full description reaches the writer as job
evidence. ``prepare-batch`` passes ``--job-listing-id``; ``apply``/``resume`` read the
listing's FULL description from the jobs store; the runner indexes it through the
knowledge store's ``index_job`` under the job the writer retrieves for, before any field
is resolved. An apply page without a description (Ashby's ``/application``) then gets a
motivation answer written instead of held.

The knowledge store and the writer are doubles; Jev is scripted; the page is a fake
browser. All data is fictional; nothing is sent anywhere."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from interviewmaxxing_browser.ai import (
    AIFormRouter,
    AIHold,
    BoundedDecisions,
    DynamicPacketResolver,
)
from interviewmaxxing_browser.ai.providers import NarrativeDraft
from interviewmaxxing_cli import main as cli_main
from interviewmaxxing_cli.batch import BatchOptions, BatchRow, default_jobs_db, listing_argv
from interviewmaxxing_cli.main import build_parser
from interviewmaxxing_cli.runner import (
    JOB_EVIDENCE_EVENT,
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
    CandidateFact,
    CandidateProfile,
    ControlType,
    FieldFillResult,
    FieldFillStatus,
    FillResult,
    JobRecord,
    LocalPaths,
    PageInspection,
    PageKind,
    SavedAnswer,
    SemanticType,
)
from interviewmaxxing_generation.knowledge import job_fingerprint
from interviewmaxxing_jobs.sources.base import make_listing
from interviewmaxxing_jobs.store import JobStore
from interviewmaxxing_selection.credentials import ApiKey
from interviewmaxxing_selection.jev import HttpResponse, JevClient

S = ApplicationState
ORIGIN = "https://jobs.ashby.test/brambleway"
NOW = datetime(2026, 9, 25, 21, 0, tzinfo=UTC)
WHY_US = "What interests you about Brambleway Analytics?"
DESCRIPTION = (
    "Brambleway Analytics builds demand forecasts for regional grocers.\n\n"
    "Own paid search and retail media strategy for enterprise brands and report results to "
    "the sales team.")
FACT_TEXT = "Managed paid search for a regional bakery chain and grew online orders by 35% (2024)."


# --- doubles ------------------------------------------------------------------------------------


class Jev:
    """Routes every question to the writer and approves every grounding question."""

    def __call__(self, url: str, headers: Any, body: bytes, timeout: float) -> HttpResponse:
        request = json.loads(body)
        answers: dict[str, Any] = {}
        for name, question in request["questions"].items():
            if question["type"] != "choice":
                answers[name] = {"type": "noul", "noul": 1.0}
                continue
            choice = ("WRITER" if name.startswith("r") and name != "route" else
                      "prose" if name.startswith("n") else
                      "HISTORICAL_OR_CONTEXTUAL" if name.startswith("u") else
                      "CUSTOM_LONG_TEXT" if name.startswith("s") else
                      "APPLICATION_ATTACHMENT" if name.startswith("d") else
                      next(iter(question["criteria"])))
            answers[name] = {"type": "choice", "choice": choice, "confidence": 1,
                             "probabilities": {c: float(c == choice) for c in question["criteria"]}}
        return HttpResponse(200, {}, json.dumps({"model": "typesafe/jev-1.13-20260917",
            "answers": answers, "usage": {"cost": 0.0001}}).encode())


@dataclass
class Knowledge:
    """The two knowledge-store calls of a run, as ``PgKnowledgeStore`` scopes them:
    ``index_job`` keeps a description's paragraphs as job chunks under the job's scope
    (the real ``job_fingerprint``: the job's normalized URL) and ``retrieve`` returns the
    chunks of the job it is asked about."""

    facts: list[CandidateFact]
    chunks: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    indexed: list[tuple[str, JobRecord, str, str]] = field(default_factory=list)
    fail: bool = False

    def index_job(self, candidate_id: str, job: JobRecord, description: str,
                  source_url: str) -> dict[str, Any]:
        if self.fail:
            raise RuntimeError("postgres://private-secret@private-host/knowledge")
        self.indexed.append((candidate_id, job, description, source_url))
        version = hashlib.sha256(description.encode()).hexdigest()
        self.chunks[job_fingerprint(job)] = [
            {"id": "job:" + hashlib.sha256(text.encode()).hexdigest(), "text": text,
             "source_url": source_url, "source_version": version}
            for text in (part.strip() for part in description.split("\n\n")) if text]
        return {"status": "indexed", "chunk_count": len(self.chunks[job_fingerprint(job)]),
                "unchanged_source_count": 0}

    def retrieve(self, *, candidate: CandidateProfile, job: JobRecord, query: str,
                 limit: int = 8, narrative: bool = False) -> Any:
        return SimpleNamespace(facts=self.facts, job_evidence=self.chunks.get(job_fingerprint(job), []),
                               voice_samples=["I write short, plain sentences."], story_chunks=[],
                               receipt={"status": "OK"})


@dataclass
class Writer:
    """Like ``NarrativeWriter.write``, a motivation answer without job evidence holds
    before any provider call; with it, one aligned draft citing the first job chunk."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    def write(self, **kwargs: Any) -> NarrativeDraft:
        if kwargs["purpose"] in ("cover_letter", "motivation") and not kwargs["job_evidence"]:
            missing = ["The actual job description, including responsibilities and requirements"]
            raise AIHold("Motivation answer needs explicit facts: " + missing[0],
                         missing_information=missing)
        self.calls.append(kwargs)
        chunk = kwargs["job_evidence"][-1]["id"]
        return NarrativeDraft.model_validate({"status": "READY", "missing_information": [], "sentences": [
            {"text": "The role owns paid search strategy and reports results to sales.",
             "job_evidence_ids": [chunk]},
            {"text": "I managed paid search for a regional bakery chain and grew online orders by 35%.",
             "fact_ids": ["fact.bakery"], "job_evidence_ids": [chunk]}]})


class OnePage:
    """An application page with one why-us TEXTAREA and no job description; the runner
    fills it and stops at the final step (preparation only)."""

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


# --- helpers ----------------------------------------------------------------------------------


@pytest.fixture
def paths(isolated_imx_home: LocalPaths) -> LocalPaths:
    isolated_imx_home.ensure()
    return isolated_imx_home


@pytest.fixture
def candidate(fictional_candidate: CandidateProfile) -> CandidateProfile:
    fact = fictional_candidate.verified_facts()[0].model_copy(update={
        "id": "fact.bakery", "key": "experience", "value": FACT_TEXT, "evidence": [FACT_TEXT]})
    return fictional_candidate.model_copy(update={"facts": [fact], "experience": [], "education": []})


def saved_listing(paths: LocalPaths, number: int, *, full: bool = True,
                  description: str | None = DESCRIPTION) -> str:
    """A fictional saved listing in the jobs store; returns its id."""
    jobs = JobStore(default_jobs_db(paths))
    try:
        posting = f"https://jobs.ashby.test/brambleway/{number:04d}"
        return jobs.upsert(make_listing(
            source="ashby", source_listing_id=f"{number:04d}", posting_url=posting,
            source_url=posting, title="Paid Media Lead", company="Brambleway Analytics",
            location="Remote (US)", observed_at=NOW, query_id="qry_round6",
            evidence="fictional round 6 observation", description=description,
            full_description=full)).id
    finally:
        jobs.close()


def listing_for(paths: LocalPaths, *argv: str) -> ListingDetails:
    """What ``apply URL ARGV`` hands the runner."""
    args = build_parser().parse_args(["apply", f"{ORIGIN}/x", *argv])
    details = cli_main._listing(args, paths)
    assert isinstance(details, ListingDetails)
    return details


def runner(paths: LocalPaths, candidate: CandidateProfile, knowledge: Knowledge,
           writer: Writer, url: str) -> LocalApplicationRunner:
    decisions = BoundedDecisions(JevClient(ApiKey("synthetic-round6-key", source="test"),
                                           transport=Jev(), max_attempts=1))
    router = AIFormRouter(decisions)
    form = ApplicationForm(url=url, step=0, is_final_step=True, submit_selector="#submit", fields=[
        ApplicationField(id="why_us", label=WHY_US, selector="#why_us", required=True,
                         semantic_type=SemanticType.CUSTOM_LONG_TEXT,
                         control_type=ControlType.TEXTAREA)])
    page = PageInspection(kind=PageKind.APPLICATION_FORM, observed_url=url,
                          form=router.annotate(form, document_id="round6-listing"))
    return LocalApplicationRunner(
        paths=paths, interaction=NoninteractiveInteraction(), headless=True,
        browser_factory=OnePageFactory(page), candidates=Candidates(candidate),
        resolver=DynamicPacketResolver(decisions, writer, router=router, retriever=knowledge),
        limits=RunLimits(max_steps=4, max_same_form=2), prepare_only=True)


def evidence_events(paths: LocalPaths, app_id: str) -> list[dict[str, Any]]:
    with ApplicationStore.open(paths.state_db) as store:
        return [e.metadata for e in store.list_events(app_id) if e.event == JOB_EVIDENCE_EVENT]


def held_ids(result: Any) -> list[str]:
    return [m.field_id for m in result.missing_inputs]


# --- the run ----------------------------------------------------------------------------------


def test_the_listing_description_reaches_the_writer_on_a_page_without_one(paths, candidate):
    listing_id = saved_listing(paths, 1)
    listing = listing_for(paths, "--job-listing-id", listing_id)
    assert listing.description == DESCRIPTION and listing.listing_id == listing_id
    assert listing.description_url == "https://jobs.ashby.test/brambleway/0001"
    knowledge, writer = Knowledge([candidate.facts[0]]), Writer()
    url = f"{ORIGIN}/0001/application"

    result = asyncio.run(runner(paths, candidate, knowledge, writer, url).apply(
        url, candidate_id="default", listing=listing))

    assert result.state is S.NEEDS_INPUT and result.message.startswith("Prepared"), result.message
    [call] = writer.calls
    assert call["purpose"] == "motivation"
    assert [e["text"] for e in call["job_evidence"]] == DESCRIPTION.split("\n\n")
    with ApplicationStore.open(paths.state_db) as store:
        job = store.get_job(store.get_application(result.application_id).job_id)
        packet = store.latest_packet(result.application_id)
    [(candidate_id, indexed_job, text, source)] = knowledge.indexed
    assert (candidate_id, text, source) == ("default", DESCRIPTION, listing.description_url)
    assert job_fingerprint(indexed_job) == job_fingerprint(job)  # the scope the writer reads
    assert packet is not None and packet.answer_for("why_us") is not None
    assert evidence_events(paths, result.application_id) == [
        {"source": "listing", "listing_id": listing_id, "status": "indexed", "chunks": 2}]


def test_without_the_listing_the_question_holds_and_a_resume_with_it_writes_it(paths, candidate):
    listing_id = saved_listing(paths, 2)
    knowledge, writer = Knowledge([candidate.facts[0]]), Writer()
    url = f"{ORIGIN}/0002/application"
    run = runner(paths, candidate, knowledge, writer, url)

    first = asyncio.run(run.apply(url, candidate_id="default"))
    assert first.state is S.NEEDS_INPUT and held_ids(first) == ["why_us"]  # as before this round
    assert writer.calls == [] and knowledge.indexed == []
    assert evidence_events(paths, first.application_id) == []

    # prepare-batch --retry runs `resume APP --job-listing-id ...`.
    again = asyncio.run(run.resume(first.application_id,
                                   listing=listing_for(paths, "--job-listing-id", listing_id)))
    assert again.state is S.NEEDS_INPUT and again.message.startswith("Prepared"), again.message
    assert len(writer.calls) == 1 and len(knowledge.indexed) == 1


def test_a_failed_index_is_recorded_and_the_question_holds_as_without_it(paths, candidate):
    listing_id = saved_listing(paths, 3)
    knowledge, writer = Knowledge([candidate.facts[0]], fail=True), Writer()
    url = f"{ORIGIN}/0003/application"
    result = asyncio.run(runner(paths, candidate, knowledge, writer, url).apply(
        url, candidate_id="default", listing=listing_for(paths, "--job-listing-id", listing_id)))
    assert result.state is S.NEEDS_INPUT and held_ids(result) == ["why_us"]
    assert writer.calls == []
    [event] = evidence_events(paths, result.application_id)
    assert event == {"source": "listing", "listing_id": listing_id, "status": "failed",
                     "error": "RuntimeError"}  # the type only, never the message
    assert "private" not in json.dumps(event)


# --- apply/resume and prepare-batch -------------------------------------------------------------


def test_apply_reads_only_a_full_description_from_the_jobs_store(paths, tmp_path, monkeypatch):
    partial = saved_listing(paths, 4, full=False)
    none = saved_listing(paths, 5, description=None)
    assert listing_for(paths).description is None  # no --job-listing-id
    for listing_id in (partial, none, "lst_not_saved"):
        details = listing_for(paths, "--job-listing-id", listing_id)
        assert (details.listing_id, details.description) == (listing_id, None)
    resumed = build_parser().parse_args(["resume", "app_x", "--job-listing-id", partial])
    assert resumed.job_listing_id == partial

    elsewhere = tmp_path / "elsewhere" / "jobs.sqlite3"
    monkeypatch.setenv("IMX_JOBS_DB", str(elsewhere))
    assert listing_for(paths, "--job-listing-id", partial).description is None
    assert not elsewhere.exists() and not elsewhere.parent.exists()  # never created


def test_prepare_batch_passes_the_listing_id_and_the_jobs_store_to_each_worker(paths, monkeypatch):
    saved = BatchRow(listing_id="lst_saved", application_url=f"{ORIGIN}/a", location="Remote (US)")
    by_url = BatchRow(listing_id=f"url:{ORIGIN}/b", application_url=f"{ORIGIN}/b")
    assert listing_argv(saved) == ["--job-listing-id", "lst_saved", "--job-location", "Remote (US)"]
    assert listing_argv(by_url) == []
    options = BatchOptions(paths=paths, candidate_id="default", batch_id="b1")
    assert options.job_argv(saved)[1:3] == ["apply", f"{ORIGIN}/a"]
    retried = saved.model_copy(update={"application_id": "app_x"})
    assert options.job_argv(retried)[-4:-2] == ["--job-listing-id", "lst_saved"]  # resume carries it
    assert options.environment(0)["IMX_JOBS_DB"] == str(paths.home / "jobs" / "jobs.sqlite3")
    monkeypatch.setenv("IMX_JOBS_DB", str(Path("/fictional/jobs.sqlite3")))
    assert options.environment(1)["IMX_JOBS_DB"] == "/fictional/jobs.sqlite3"
