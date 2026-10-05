"""Real SQLite/lifecycle tests with deterministic provider stand-ins, never user performance."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from interviewmaxxing_service.errors import ApiError
from interviewmaxxing_service.interviews import DIMENSIONS, InterviewApi, validate_score


class Providers:
    fail = False
    calls = 0
    def readiness(self):
        return {"textReady": True, "voiceReady": False}
    def question(self, context, turns):
        return "What was your owned contribution and measured result?"
    def score(self, context, question, answer):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private provider secret never rendered")
        return {"dimensions": dict.fromkeys(DIMENSIONS, 75), "model": "typesafe/jev-1.13",
                "rubricVersion": "interview-v1", "confidence": dict.fromkeys(DIMENSIONS, .8),
                "evidence": [{"quote": answer, "dimension": "evidence"}]}
    def transcribe(self, content, filename):
        return "Previous interviewer challenged attribution of the 42 percent CAC improvement."
    def connect(self, session):
        return {"url": "ws://localhost:7880", "token": "fixture", "roomName": session["id"]}


@pytest.fixture
def api(tmp_path):
    api = InterviewApi(tmp_path / "interviews.sqlite3", Providers())
    yield api
    api.close()


def create(api, **kw):
    return api.create({"company": "Fixture Co", "title": "Growth lead", "jobDescription": "Own paid marketing acquisition", **kw})


def judged(api, sid):
    for _ in range(100):
        session = api.get(sid)
        if session["turns"] and session["turns"][-1]["scoreStatus"] != "pending":
            return session
        time.sleep(.01)
    raise AssertionError("judgment did not finish")


@pytest.mark.parametrize("round_number", [2, 3])
def test_later_round_without_prior_material_can_practice_and_score(api, round_number):
    session = create(api, round=round_number, resumeText="Managed paid search experiments.")
    sid = session["id"]
    assert not any(d["kind"] in {"prior_recording", "prior_transcript"} for d in session["documents"])
    assert api.start(sid)["status"] == "active"
    assert api.connect(sid)["roomName"] == sid
    api.voice_turn(sid, "What did you measure?", "I compared qualified leads in a holdout experiment.", "no-prior")
    done = judged(api, sid)
    assert done["turns"][0]["scoreStatus"] == "completed"
    assert {r["kind"] for r in done["turns"][0]["retrieval"]} >= {"job", "resume"}
    assert api.finish(sid)["report"]["gradedAnswers"] == 1


def test_durable_context_repeat_and_async_score(api):
    session = create(api)
    sid = session["id"]
    api.add_document(sid, "prior_transcript", "round-one.txt", "The disputed CAC attribution was 42 percent.")
    assert "42 percent" in api.context(sid, "CAC attribution")
    started = api.start(sid)
    assert api.start(sid)["deadlineAt"] == started["deadlineAt"]
    api.voice_turn(sid, "Explain CAC attribution", "I randomized treatment and holdout accounts.", "one")
    done = judged(api, sid)
    assert done["turns"][0]["score"]["overallScore"] == 75
    assert any(r["name"] == "round-one.txt" for r in done["turns"][0]["retrieval"])
    assert done["currentQuestion"] is None  # actual voice runtime owns next spoken question
    report = api.finish(sid)["report"]
    assert report["overallScore"] == 75 and not report["coverage"]["fullDuration"]
    repeat = api.create({"previousSessionId": sid, "jobDescription": "NEW ROLE CONTEXT"})
    context = api.context(repeat["id"])
    assert "NEW ROLE CONTEXT" in context and "Own paid marketing acquisition" not in context
    assert "42 percent" in context
    reopened = InterviewApi(api.path, Providers())
    try:
        assert reopened.get(sid)["report"] == report
    finally:
        reopened.close()


def test_retry_failed_judgment_does_not_duplicate_answer(api):
    sid = create(api)["id"]
    api.start(sid)
    api.providers.fail = True
    api.voice_turn(sid, "Metric?", "42 percent", "retry")
    failed = judged(api, sid)
    assert failed["turns"][0]["score"] is None
    assert "secret" not in failed["turns"][0]["error"]
    assert api.finish(sid)["report"]["overallScore"] is None
    api.providers.fail = False
    api.voice_turn(sid, "Metric?", "42 percent", "retry")
    done = judged(api, sid)
    assert len(done["turns"]) == 1 and done["report"]["overallScore"] == 75
    with pytest.raises(ApiError):
        api.voice_turn(sid, "Metric?", "different", "retry")


def test_deadline_and_single_active_cross_connection(api):
    sid = create(api)["id"]
    second = create(api)["id"]
    api.start(sid)
    other = InterviewApi(api.path, Providers())
    try:
        with pytest.raises(ApiError):
            other.start(second)
        api._mutate(sid, lambda s: s.update(deadlineAt=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()))
        with pytest.raises(ApiError):
            other.voice_turn(sid, "Q", "A", "late")
        assert other.get(sid)["status"] == "completed"
        other.start(second)
    finally:
        other.close()


def test_concurrent_idempotency(api):
    sid = create(api)["id"]
    api.start(sid)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: api.voice_turn(sid, "Q", "A", "same"), range(8)))
    assert len(judged(api, sid)["turns"]) == 1
    assert api.providers.calls == 1


def test_upload_validation_and_transcription(api):
    sid = create(api)["id"]
    session = api.upload(sid, "prior_recording", "round.wav", b"RIFFfake-fixture")
    assert session["documents"][-1]["chunkCount"] > 0
    for name, data in [("../../private.wav", b"data"), ("bad.exe", b"data"), ("empty.wav", b"")]:
        with pytest.raises(ApiError):
            api.upload(sid, "prior_recording", name, data)
    with pytest.raises(ApiError):
        api.add_document(sid, "notes", "x", "\x00bad")


def test_score_validation_preserves_real_jev_when_feedback_fails():
    score = Providers().score("", "Q", "A")
    score["evidence"] = []
    assert validate_score(score)["overallScore"] == 75
    score["dimensions"]["depth"] = 101
    with pytest.raises(ValueError):
        validate_score(score)


def test_api_security_and_persistence(harness):
    client = harness.client
    response = client.post("/interviews", {"company": "Fixture", "title": "Lead"})
    assert response.status == 201
    sid = response.json["id"]
    assert client.get(f"/interviews/{sid}").json["title"] == "Lead"
    assert client.post(f"/interviews/{sid}/finish", origin="https://evil.example").status == 403
    assert client.get("/interviews", host="evil.example").status == 403
    assert client.post("/interviews", {"company": "Fixture", "durationMinutes": 0}).status == 400
    assert client.post(f"/interviews/{sid}/documents", {"kind": "notes", "name": "note", "text": "Grounded"}).status == 200


def test_docx_safe_text_and_invalid_pdf(api):
    import io
    import zipfile
    sid = create(api)["id"]
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Owned campaign economics</w:t></w:r></w:p></w:body></w:document>')
    result = api.upload(sid, "resume", "resume.docx", docx.getvalue())
    assert result["documents"][-1]["chunkCount"] == 1
    assert "Owned campaign economics" in api.context(sid)
    with pytest.raises(ApiError):
        api.upload(sid, "resume", "resume.pdf", b"not a PDF")


def test_transcription_failure_persists_recoverable_document(api):
    sid = create(api)["id"]
    original = api.providers.transcribe
    def fail(*args):
        raise RuntimeError("secret")
    api.providers.transcribe = fail
    failed = api.upload(sid, "prior_recording", "prior.wav", b"fixture")
    doc = failed["documents"][-1]
    assert doc["status"] == "failed" and "secret" not in doc["error"]
    api.providers.transcribe = original
    recovered = api.upload(sid, "prior_recording", "prior.wav", b"fixture")
    assert recovered["documents"][-1]["id"] == doc["id"]
    assert recovered["documents"][-1]["status"] == "ready"
    count = len(recovered["documents"])
    assert len(api.upload(sid, "prior_recording", "prior.wav", b"fixture")["documents"]) == count


def test_repeat_changed_setup_preserves_uploaded_notes_and_resume(api):
    original = create(api, notes="Original setup note")
    sid = original["id"]
    api.add_document(sid, "notes", "Company research.txt", "Uploaded company evidence")
    api.add_document(sid, "resume", "Selected resume.txt", "Uploaded candidate evidence")
    api.finish(sid)
    repeat = api.create({"previousSessionId": sid, "notes": "Revised setup note", "resumeText": ""})
    context = api.context(repeat["id"])
    assert "Revised setup note" in context
    assert "Original setup note" not in context
    assert "Uploaded company evidence" in context
    assert "Uploaded candidate evidence" in context
    assert any(d["name"] == "Company research.txt" and d["origin"] == "upload" for d in repeat["documents"])
    assert "Original setup note" in api.context(sid)


class ScrapeProviders(Providers):
    def __init__(self):
        import threading
        self.job_calls = 0
        self.company_calls = 0
        self.company_fails = False
        self.release = threading.Event()
        self.release.set()

    def scrape_job(self, url):
        self.job_calls += 1
        assert self.release.wait(3), "fixture scrape timeout"
        return {"text": "Scraped responsibilities for " + url, "title": "Job page", "url": url}

    def scrape_company(self, url):
        from interviewmaxxing_service.interview_scraping import ScrapeError
        self.company_calls += 1
        if self.company_fails:
            raise ScrapeError("Configure FIRECRAWL_API_KEY in the private environment file.")
        return [{"text": "Company builds attribution software", "title": "Company home", "url": url},
                {"text": "Company product uses causal measurement", "title": "Product", "url": url.rstrip("/") + "/product"}]


def ingested(api, sid):
    for _ in range(200):
        session = api.get(sid)
        if session["sourceIngestion"]["status"] != "processing":
            return session
        time.sleep(.01)
    raise AssertionError("ingestion did not finish")


def test_async_website_ingestion_blocks_start_and_retrieves_company(api, monkeypatch):
    monkeypatch.setattr("interviewmaxxing_service.interviews.validate_public_url", lambda value: value)
    provider = api.providers = ScrapeProviders()
    provider.release.clear()
    session = create(api, jobUrl="https://example.com/job", companyUrl="https://example.com")
    sid = session["id"]
    try:
        assert session["sourceIngestion"]["status"] == "processing"
        with pytest.raises(ApiError):
            api.start(sid)
    finally:
        provider.release.set()
    ready = ingested(api, sid)
    assert ready["sourceIngestion"]["status"] == "ready"
    assert ready["jobDescription"] == "Scraped responsibilities for https://example.com/job"
    web = [d for d in ready["documents"] if d.get("origin") == "web"]
    assert len(web) == 3 and all(d["sourceUrl"].startswith("https://example.com") for d in web)
    retrieval = api.retrieval(sid, "causal measurement")
    assert any(d["kind"] == "company" and d["sourceUrl"] for d in retrieval)
    assert "companyUrl" in api.context(sid)
    assert api.start(sid)["status"] == "active"


def test_partial_ingestion_retry_reuses_job_and_preserves_uploads(api, monkeypatch):
    monkeypatch.setattr("interviewmaxxing_service.interviews.validate_public_url", lambda value: value)
    provider = api.providers = ScrapeProviders()
    provider.company_fails = True
    sid = create(api, jobUrl="https://example.com/job", companyUrl="https://example.com")["id"]
    failed = ingested(api, sid)
    assert failed["sourceIngestion"]["jobStatus"] == "ready"
    assert failed["sourceIngestion"]["companyStatus"] == "failed"
    assert "FIRECRAWL_API_KEY" in failed["sourceIngestion"]["error"]
    with pytest.raises(ApiError):
        api.start(sid)
    api.add_document(sid, "notes", "private-notes.txt", "Preserve uploaded source")
    provider.company_fails = False
    api.ingest(sid)
    ready = ingested(api, sid)
    assert ready["sourceIngestion"]["status"] == "ready"
    assert provider.job_calls == 1 and provider.company_calls == 2
    assert len([d for d in ready["documents"] if d.get("origin") == "web"]) == 3
    assert "Preserve uploaded source" in api.context(sid)
    api.ingest(sid)
    assert provider.job_calls == 1 and provider.company_calls == 2


def test_repeat_ready_urls_reuses_scrapes_changed_job_purges_stale_context(api, monkeypatch):
    monkeypatch.setattr("interviewmaxxing_service.interviews.validate_public_url", lambda value: value)
    provider = api.providers = ScrapeProviders()
    sid = create(api, jobUrl="https://example.com/old-job", companyUrl="https://example.com")["id"]
    ingested(api, sid)
    api.add_document(sid, "notes", "notes.txt", "Persistent interview notes")
    repeat = api.create({"previousSessionId": sid})
    assert repeat["sourceIngestion"]["status"] == "ready"
    assert provider.job_calls == provider.company_calls == 1
    provider.release.clear()
    changed = api.create({"previousSessionId": sid, "jobUrl": "https://example.com/new-job"})
    try:
        assert changed["jobDescription"] == ""
        assert not any(d.get("sourceUrl") == "https://example.com/old-job" for d in changed["documents"])
        assert any(d["kind"] == "company" for d in changed["documents"])
    finally:
        provider.release.set()
    ready = ingested(api, changed["id"])
    assert "new-job" in ready["jobDescription"]
    assert provider.company_calls == 1 and provider.job_calls == 2
    assert "Persistent interview notes" in api.context(changed["id"])
    assert "old-job" in api.get(sid)["jobDescription"]


def test_legacy_session_and_source_retry_route(harness, monkeypatch):
    api = harness.app.interviews
    monkeypatch.setattr("interviewmaxxing_service.interviews.validate_public_url", lambda value: value)
    api.providers = ScrapeProviders()
    legacy = create(api)
    def remove_fields(session):
        session.pop("companyUrl")
        session.pop("sourceIngestion")
    api._mutate(legacy["id"], remove_fields)
    assert api.get(legacy["id"])["companyUrl"] == ""
    assert api.start(legacy["id"])["status"] == "active"
    api.finish(legacy["id"])
    new = harness.client.post("/interviews", {"company": "Fixture", "title": "Role", "jobUrl": "https://example.com/job", "companyUrl": "https://example.com"})
    assert new.status == 201
    sid = new.json["id"]
    ingested(api, sid)
    assert harness.client.post(f"/interviews/{sid}/ingest").status == 200
    assert harness.client.post(f"/interviews/{sid}/ingest", origin="https://evil.example").status == 403


def test_new_company_url_contract_requires_both_urls(api, monkeypatch):
    monkeypatch.setattr("interviewmaxxing_service.interviews.validate_public_url", lambda value: value)
    for payload in ({"companyUrl": "", "jobUrl": "https://example.com/job"},
                    {"companyUrl": "https://example.com", "jobUrl": ""}):
        with pytest.raises(ApiError, match="Company website and job application URLs are required"):
            create(api, **payload)
    # Existing API callers omit the newly introduced field and stay compatible.
    assert create(api)["sourceIngestion"]["status"] == "ready"


def test_changed_job_url_removes_legacy_unmarked_job_chunks(api, monkeypatch):
    monkeypatch.setattr("interviewmaxxing_service.interviews.validate_public_url", lambda value: value)
    api.providers = provider = ScrapeProviders()
    original = create(api, jobDescription="Legacy job responsibilities")
    sid = original["id"]
    def remove_job_origin(session):
        for document in session["documents"]:
            if document["kind"] == "job":
                document.pop("origin", None)
    api._mutate(sid, remove_job_origin)
    provider.release.clear()
    changed = api.create({"previousSessionId": sid, "jobUrl": "https://example.com/new-job"})
    try:
        assert "Legacy job responsibilities" not in api.context(changed["id"])
    finally:
        provider.release.set()
    assert "Legacy job responsibilities" not in api.context(ingested(api, changed["id"])["id"])
    assert "Legacy job responsibilities" in api.context(sid)


def test_opening_retrieval_reserves_every_company_page_with_long_job(api, monkeypatch):
    monkeypatch.setattr("interviewmaxxing_service.interviews.validate_public_url", lambda value: value)
    provider = api.providers = ScrapeProviders()
    provider.scrape_job = lambda url: {"text": "Responsibilities and operating details. " * 2000, "title": "Long job", "url": url}
    provider.scrape_company = lambda url: [{"text": f"Company page {name} evidence", "title": name, "url": url + "/" + name}
                                           for name in ("home", "about", "product", "customers", "pricing")]
    sid = create(api, jobUrl="https://example.com/job", companyUrl="https://example.com")["id"]
    ingested(api, sid)
    retrieved = api.retrieval(sid)
    assert len(retrieved) <= 12
    assert {chunk["name"] for chunk in retrieved if chunk["kind"] == "company"} == {"home", "about", "product", "customers", "pricing"}
    assert any(chunk["kind"] == "job" for chunk in retrieved)
